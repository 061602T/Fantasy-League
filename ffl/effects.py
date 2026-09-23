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
  * ``trade_freeze`` / ``waiver_backseat`` RECORD their state in ``team_effects``
    with an ``active_through_week``, but the enforcement hooks (skipping a frozen
    team in the trade loop, penalising a back-seated team's waiver ties) are
    deliberately NOT wired into the live tick loop yet -- that is a separate,
    reviewed step. Until then they are recorded and queryable but inert.

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


# --- registry ---------------------------------------------------------------

# effect_type -> (validate(conn, team_id, params)->(ok,err),
#                 apply(conn, team_id, params, bylaw_id)->summary)
EFFECTS = {
    "faab_adjust":     (_v_faab, _a_faab),
    "trade_freeze":    (_v_weeks(config.GOV_FREEZE_MAX_WEEKS),
                        _a_duration("trade_freeze")),
    "waiver_backseat": (_v_weeks(config.GOV_BACKSEAT_MAX_WEEKS),
                        _a_duration("waiver_backseat")),
    "loser_flag":      (_v_loser, _a_loser),
    "late_fee":        (_v_late_fee, _a_late_fee),
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
    "loser_flag": {
        "desc": "attach a display-only shame label to one team",
        "params": {"label": ("str", "short text label")},
    },
    "late_fee": {
        "desc": "fine one team a capped amount, paid directly to a named "
                "opponent (e.g. a late-lineup fee)",
        "params": {
            "opponent": ("str", "opposing team name that receives the fee"),
            "amount": ("int", f"integer 1 to {config.GOV_LATE_FEE_MAX}"),
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
