"""Bounded 'effects' toolbox for governance enactment.

When a free-form bylaw passes its vote, nothing runs automatically. The
commissioner reviews it (scripts/review_bylaws.py) and may enact it either as
displayed lore (no mechanical change) or, if it deserves teeth, as exactly ONE
of the effects below. These are the ONLY mechanical changes the governance
system can make, and each validates its own parameters with the same defensive
rigor as the draft engine's caps and the trade validator -- so even a manual
enactment cannot push a budget negative, freeze trades forever, or target a team
that doesn't exist.

What takes effect when:
  * ``faab_adjust`` changes ``teams.faab_remaining`` immediately on enactment
    (clamped), and ``loser_flag`` stores a display flag immediately.
  * ``late_fee`` immediately transfers a capped FAAB amount from the offending
    team to a named opponent (floored so the payer never goes negative) and
    records a ``team_effects`` row for the week, for a weekly tally.
  * ``waiver_forfeit`` immediately transfers a capped FAAB amount from a team
    that claimed during another team's declared priority window to that
    wronged team (floored so the violator never goes negative), same shape as
    ``late_fee``, recorded as its own ``team_effects`` type for a separate
    tally.
  * ``late_lineup_tax`` immediately moves a capped percentage of a team's
    already-scored week's points to that week's best-roster-efficiency team --
    found automatically, never a human-picked recipient (``ffl/season.py``:
    ``best_efficiency_team`` / ``team_week_efficiency``) -- by adjusting the
    two teams' final ``matchups`` rows and rebuilding standings.
  * ``trade_freeze`` / ``waiver_backseat`` / ``kicker_flex_lock`` /
    ``worst_lineup_lock`` / ``chat_mute`` RECORD their state in ``team_effects``
    with an ``active_through_week``; their enforcement hooks read it back via
    ``active_team_ids`` -- skipping a frozen team in the trade loop
    (``ffl/market.py:negotiate``), penalising a back-seated team's waiver ties
    (``ffl/market.py:_waiver_priority_key``), forcing a locked team's kicker
    into FLEX or its lineup to the worst-projected eligible players when its
    weekly lineup is set (``ffl/season.py:set_lineup``), and silencing a muted
    team in group chat (``ffl/chat.py``).

The model never reaches this code: a GM's bylaw is free text, and the
commissioner -- a human -- chooses which effect (if any) to apply. This module
only guards the human's toolbox.
"""
from __future__ import annotations

import json
import re

from . import config


# --- helpers ---------------------------------------------------------------

def sanitize(s, maxlen: int) -> str:
    """Collapse whitespace, strip control characters, and cap length. Used for
    every piece of model-authored text that gets stored or displayed."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxlen].strip()


def _current_week(conn) -> int:
    row = conn.execute("SELECT current_week FROM league WHERE id=1").fetchone()
    return int(row["current_week"]) if row and row["current_week"] else 1


def _team(conn, team_id):
    return conn.execute(
        "SELECT team_id, team_name, faab_remaining FROM teams WHERE team_id=?",
        (team_id,)).fetchone()


def team_id_by_name(conn, name: str):
    row = conn.execute("SELECT team_id FROM teams WHERE team_name=?",
                       (name,)).fetchone()
    return row["team_id"] if row else None


def active_team_ids(conn, effect_type: str, week: int) -> set:
    """team_ids that currently have an active effect of this type. A duration
    effect is active while its `active_through_week` is >= `week`; a NULL through
    week means season-long / display (always active). Read side for the trade and
    waiver enforcement hooks."""
    rows = conn.execute(
        "SELECT DISTINCT team_id FROM team_effects WHERE effect_type=? AND "
        "(active_through_week IS NULL OR active_through_week >= ?)",
        (effect_type, week)).fetchall()
    return {r["team_id"] for r in rows}


# --- effect: faab_adjust ----------------------------------------------------

def _v_faab(conn, team_id, p):
    if _team(conn, team_id) is None:
        return False, "no such team"
    try:
        delta = int(p["delta"])
    except (KeyError, TypeError, ValueError):
        return False, "delta must be an integer"
    if delta == 0:
        return False, "delta must be non-zero"
    if abs(delta) > config.GOV_FAAB_MAX_DELTA:
        return False, f"|delta| must be <= {config.GOV_FAAB_MAX_DELTA}"
    return True, ""


def _a_faab(conn, team_id, p, bylaw_id):
    t = _team(conn, team_id)
    delta, cur = int(p["delta"]), t["faab_remaining"]
    # Clamp to [0, max(cap, current)] -- a team that traded ABOVE the starting
    # cap keeps that surplus; we only refuse to go negative or inflate past what
    # they already legitimately hold.
    cap = max(config.FAAB_BUDGET, cur)
    new = max(0, min(cur + delta, cap))
    conn.execute("UPDATE teams SET faab_remaining=? WHERE team_id=?", (new, team_id))
    return f"{t['team_name']} FAAB {cur} -> {new} (delta {delta:+d})"


# --- effects: trade_freeze / waiver_backseat (duration) ---------------------

def _v_weeks(max_weeks):
    def v(conn, team_id, p):
        if _team(conn, team_id) is None:
            return False, "no such team"
        try:
            w = int(p["weeks"])
        except (KeyError, TypeError, ValueError):
            return False, "weeks must be an integer"
        if not (1 <= w <= max_weeks):
            return False, f"weeks must be between 1 and {max_weeks}"
        return True, ""
    return v


def _a_duration(effect_type):
    def a(conn, team_id, p, bylaw_id):
        w = int(p["weeks"])
        through = _current_week(conn) + w
        conn.execute(
            "INSERT INTO team_effects(team_id, effect_type, params_json, "
            "active_through_week, bylaw_id) VALUES(?,?,?,?,?)",
            (team_id, effect_type, json.dumps({"weeks": w}), through, bylaw_id))
        t = _team(conn, team_id)
        return (f"{t['team_name']}: {effect_type} recorded through week {through} "
                f"({w}w) -- enforcement hook pending")
    return a


# --- effect: loser_flag (narrative / display) -------------------------------

def _v_loser(conn, team_id, p):
    if _team(conn, team_id) is None:
        return False, "no such team"
    if not sanitize(p.get("label", ""), config.GOV_LOSER_LABEL_MAX):
        return False, "label required"
    return True, ""


def _a_loser(conn, team_id, p, bylaw_id):
    label = sanitize(p.get("label", ""), config.GOV_LOSER_LABEL_MAX)
    conn.execute(
        "INSERT INTO team_effects(team_id, effect_type, params_json, "
        "active_through_week, bylaw_id) VALUES(?,?,?,?,?)",
        (team_id, "loser_flag", json.dumps({"label": label}), None, bylaw_id))
    t = _team(conn, team_id)
    return f'{t["team_name"]} loser flag: "{label}"'


# --- effect: late_fee (immediate, two-team transfer) ------------------------

def _v_late_fee(conn, team_id, p):
    if _team(conn, team_id) is None:
        return False, "no such team"
    opp_name = str(p.get("opponent", "")).strip()
    if not opp_name:
        return False, "opponent required"
    opp_id = team_id_by_name(conn, opp_name)
    if opp_id is None:
        return False, f"no such opponent team {opp_name!r}"
    if opp_id == team_id:
        return False, "opponent must be a different team"
    try:
        amount = int(p["amount"])
    except (KeyError, TypeError, ValueError):
        return False, "amount must be an integer"
    if not (1 <= amount <= config.GOV_LATE_FEE_MAX):
        return False, f"amount must be between 1 and {config.GOV_LATE_FEE_MAX}"
    return True, ""


def _a_late_fee(conn, team_id, p, bylaw_id):
    t = _team(conn, team_id)
    opp_id = team_id_by_name(conn, str(p["opponent"]).strip())
    opp = _team(conn, opp_id)
    amount = int(p["amount"])
    # Cap the transfer at what the payer actually has -- a fine can never push
    # the payer's FAAB negative (same floor rule as faab_adjust).
    paid = max(0, min(amount, t["faab_remaining"]))
    conn.execute("UPDATE teams SET faab_remaining=faab_remaining-? WHERE team_id=?",
                (paid, team_id))
    conn.execute("UPDATE teams SET faab_remaining=faab_remaining+? WHERE team_id=?",
                (paid, opp_id))
    conn.execute(
        "INSERT INTO team_effects(team_id, effect_type, params_json, "
        "active_through_week, bylaw_id) VALUES(?,?,?,?,?)",
        (team_id, "late_fee",
         json.dumps({"opponent_team_id": opp_id, "amount": paid}),
         _current_week(conn), bylaw_id))
    capped = " (capped, payer had less)" if paid < amount else ""
    return f"{t['team_name']} pays a ${paid} late fee to {opp['team_name']}{capped}"


# --- effect: waiver_forfeit (immediate, two-team transfer) ------------------
# Bylaw #11: a GM who submits a waiver claim during another GM's declared
# priority window forfeits a capped amount of their next FAAB claim to the
# wronged team. The "priority window" itself isn't a tracked game object --
# same as late_fee, the commissioner validates the infraction by hand before
# enacting -- so this is an immediate transfer with no separate enforcement
# hook.

def _v_waiver_forfeit(conn, team_id, p):
    if _team(conn, team_id) is None:
        return False, "no such team"
    opp_name = str(p.get("opponent", "")).strip()
    if not opp_name:
        return False, "opponent required"
    opp_id = team_id_by_name(conn, opp_name)
    if opp_id is None:
        return False, f"no such opponent team {opp_name!r}"
    if opp_id == team_id:
        return False, "opponent must be a different team"
    try:
        amount = int(p["amount"])
    except (KeyError, TypeError, ValueError):
        return False, "amount must be an integer"
    if not (1 <= amount <= config.GOV_WAIVER_FORFEIT_MAX):
        return False, f"amount must be between 1 and {config.GOV_WAIVER_FORFEIT_MAX}"
    return True, ""


def _a_waiver_forfeit(conn, team_id, p, bylaw_id):
    t = _team(conn, team_id)
    opp_id = team_id_by_name(conn, str(p["opponent"]).strip())
    opp = _team(conn, opp_id)
    amount = int(p["amount"])
    # Cap the forfeit at what the violator actually has -- same floor rule as
    # late_fee/faab_adjust, a penalty can never push a team's FAAB negative.
    paid = max(0, min(amount, t["faab_remaining"]))
    conn.execute("UPDATE teams SET faab_remaining=faab_remaining-? WHERE team_id=?",
                (paid, team_id))
    conn.execute("UPDATE teams SET faab_remaining=faab_remaining+? WHERE team_id=?",
                (paid, opp_id))
    conn.execute(
        "INSERT INTO team_effects(team_id, effect_type, params_json, "
        "active_through_week, bylaw_id) VALUES(?,?,?,?,?)",
        (team_id, "waiver_forfeit",
         json.dumps({"opponent_team_id": opp_id, "amount": paid}),
         _current_week(conn), bylaw_id))
    capped = " (capped, violator had less)" if paid < amount else ""
    return (f"{t['team_name']} forfeits ${paid} FAAB to {opp['team_name']} "
            f"for jumping their priority window{capped}")


# --- effect: late_lineup_tax (immediate, points transfer) -------------------
# Bylaw #12: a GM who submits a lineup change within 60 minutes of kickoff, or
# starts a bye-week player, forfeits a capped percentage of that already-scored
# week's points to the week's best roster-efficiency GM. Unlike late_fee/
# waiver_forfeit, the recipient is NOT named by the commissioner -- it's found
# automatically from the week's actual results (ffl/season.py:
# best_efficiency_team), so this effect can't be pointed at an arbitrary team.
# "One transfer per team per week" is enforced by refusing a second enactment
# of this type for the same (team, week).

def _v_late_lineup_tax(conn, team_id, p):
    if _team(conn, team_id) is None:
        return False, "no such team"
    try:
        week = int(p["week"])
    except (KeyError, TypeError, ValueError):
        return False, "week must be an integer"
    if not (1 <= week <= config.REGULAR_SEASON_WEEKS):
        return False, f"week must be between 1 and {config.REGULAR_SEASON_WEEKS}"
    try:
        pct = int(p["pct"])
    except (KeyError, TypeError, ValueError):
        return False, "pct must be an integer"
    if not (1 <= pct <= config.GOV_LATE_LINEUP_TAX_MAX_PCT):
        return False, f"pct must be between 1 and {config.GOV_LATE_LINEUP_TAX_MAX_PCT}"
    if conn.execute(
            "SELECT 1 FROM team_effects WHERE team_id=? AND effect_type="
            "'late_lineup_tax' AND active_through_week=?", (team_id, week)).fetchone():
        return False, f"team already taxed for week {week} (one per team per week)"
    m = conn.execute(
        "SELECT home_team_id, home_points, away_points FROM matchups WHERE "
        "week=? AND status='final' AND (home_team_id=? OR away_team_id=?)",
        (week, team_id, team_id)).fetchone()
    if m is None:
        return False, f"no finalized matchup for that team in week {week}"
    payer_points = m["home_points"] if m["home_team_id"] == team_id else m["away_points"]
    if not payer_points or payer_points <= 0:
        return False, f"team scored 0 or fewer points in week {week}; nothing to tax"
    from . import season  # local import: season.py imports this module
    if season.best_efficiency_team(conn, week, exclude_team_id=team_id) is None:
        return False, f"no eligible recipient team found for week {week}"
    return True, ""


def _a_late_lineup_tax(conn, team_id, p, bylaw_id):
    from . import season  # local import: season.py imports this module
    t = _team(conn, team_id)
    week, pct = int(p["week"]), int(p["pct"])
    result = season.apply_points_tax(conn, team_id, week, pct)
    amount, recipient_id = result["amount"], result["recipient_team_id"]
    recipient = _team(conn, recipient_id)
    conn.execute(
        "INSERT INTO team_effects(team_id, effect_type, params_json, "
        "active_through_week, bylaw_id) VALUES(?,?,?,?,?)",
        (team_id, "late_lineup_tax",
         json.dumps({"week": week, "pct": pct, "amount": amount,
                    "recipient_team_id": recipient_id}),
         week, bylaw_id))
    return (f"{t['team_name']} forfeits {amount} pts ({pct}% of week {week}) to "
            f"{recipient['team_name']} (best roster efficiency)")


# --- registry ---------------------------------------------------------------

# effect_type -> (validate(conn, team_id, params)->(ok,err),
#                 apply(conn, team_id, params, bylaw_id)->summary)
EFFECTS = {
    "faab_adjust":     (_v_faab, _a_faab),
    "trade_freeze":    (_v_weeks(config.GOV_FREEZE_MAX_WEEKS),
                        _a_duration("trade_freeze")),
    "waiver_backseat": (_v_weeks(config.GOV_BACKSEAT_MAX_WEEKS),
                        _a_duration("waiver_backseat")),
    "kicker_flex_lock": (_v_weeks(config.GOV_KICKER_FLEX_MAX_WEEKS),
                        _a_duration("kicker_flex_lock")),
    "worst_lineup_lock": (_v_weeks(config.GOV_WORST_LINEUP_MAX_WEEKS),
                        _a_duration("worst_lineup_lock")),
    "chat_mute":       (_v_weeks(config.GOV_CHAT_MUTE_MAX_WEEKS),
                        _a_duration("chat_mute")),
    "loser_flag":      (_v_loser, _a_loser),
    "late_fee":        (_v_late_fee, _a_late_fee),
    "waiver_forfeit":  (_v_waiver_forfeit, _a_waiver_forfeit),
    "late_lineup_tax": (_v_late_lineup_tax, _a_late_lineup_tax),
}

# Human/model-facing metadata for each effect, used by the commissioner's
# auto-picker (governance.suggest_effect / --auto). Keep one entry per EFFECTS
# key. `params` maps each accepted parameter to (kind, description-with-bounds),
# where kind is "int" or "str". A NEW effect the coding agent adds must add its
# entry here too -- that (and only that) is what lets `review_bylaws --auto`
# offer it automatically, so nothing hard-codes the effect list anymore.
EFFECT_META = {
    "faab_adjust": {
        "desc": "change one team's FAAB waiver budget (negative = a penalty)",
        "params": {"delta": ("int",
                   f"non-zero integer from -{config.GOV_FAAB_MAX_DELTA} to "
                   f"{config.GOV_FAAB_MAX_DELTA}")},
    },
    "trade_freeze": {
        "desc": "bar one team from making trades for a while",
        "params": {"weeks": ("int", f"integer 1 to {config.GOV_FREEZE_MAX_WEEKS}")},
    },
    "waiver_backseat": {
        "desc": "send one team to the back of every waiver tie for a while",
        "params": {"weeks": ("int", f"integer 1 to {config.GOV_BACKSEAT_MAX_WEEKS}")},
    },
    "kicker_flex_lock": {
        "desc": "force one team's kicker into the FLEX slot (K sits empty) for a while",
        "params": {"weeks": ("int", f"integer 1 to {config.GOV_KICKER_FLEX_MAX_WEEKS}")},
    },
    "worst_lineup_lock": {
        "desc": "force one team's worst-projected eligible players into its "
                "starting lineup for a week -- their studs sit, as if benched "
                "(Bylaw #10: a late/careless-lineup penalty)",
        "params": {"weeks": ("int",
                   f"integer 1 to {config.GOV_WORST_LINEUP_MAX_WEEKS}")},
    },
    "loser_flag": {
        "desc": "attach a display-only shame label to one team",
        "params": {"label": ("str", "short text label")},
    },
    "chat_mute": {
        "desc": "revoke one team's league group-chat posting privileges for a while",
        "params": {"weeks": ("int", f"integer 1 to {config.GOV_CHAT_MUTE_MAX_WEEKS}")},
    },
    "late_fee": {
        "desc": "fine one team a capped amount, paid directly to a named "
                "opponent (e.g. a late-lineup fee)",
        "params": {
            "opponent": ("str", "opposing team name that receives the fee"),
            "amount": ("int", f"integer 1 to {config.GOV_LATE_FEE_MAX}"),
        },
    },
    "waiver_forfeit": {
        "desc": "forfeit a capped FAAB amount from one team to a named "
                "opponent for claiming during that opponent's declared "
                "waiver priority window",
        "params": {
            "opponent": ("str", "wronged team name that receives the forfeit"),
            "amount": ("int", f"integer 1 to {config.GOV_WAIVER_FORFEIT_MAX}"),
        },
    },
    "late_lineup_tax": {
        "desc": "forfeit a percentage of one team's already-scored week points "
                "to that week's best roster-efficiency team, found "
                "automatically (Bylaw #12: a late lineup change or a "
                "benched-bye-week player)",
        "params": {
            "week": ("int", f"integer 1 to {config.REGULAR_SEASON_WEEKS}, "
                     "must already be scored"),
            "pct": ("int", f"integer 1 to {config.GOV_LATE_LINEUP_TAX_MAX_PCT}"),
        },
    },
}


def validate_effect(conn, effect_type, team_id, params) -> tuple[bool, str]:
    if effect_type not in EFFECTS:
        return False, f"unknown effect '{effect_type}'"
    return EFFECTS[effect_type][0](conn, team_id, params)


def apply_effect(conn, effect_type, team_id, params,
                 bylaw_id=None) -> tuple[bool, str]:
    """Re-validate then apply. Returns (ok, summary-or-error). Commits on
    success; on a validation failure nothing is written."""
    ok, err = validate_effect(conn, effect_type, team_id, params)
    if not ok:
        return False, err
    summary = EFFECTS[effect_type][1](conn, team_id, params, bylaw_id)
    conn.commit()
    return True, summary
