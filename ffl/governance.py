"""Free-form GM governance: propose -> discuss -> vote -> (your) enactment.

GMs propose bylaws / punishments in plain language, argue in character, and
vote. The design (chosen deliberately, see the session history):

  * Proposals and votes are FREE-FORM natural language -- there is no menu the
    model picks from. Every proposal is text.
  * A passed vote NEVER auto-executes. It moves to 'passed_pending' and waits
    for the commissioner, who enacts it via scripts/review_bylaws.py either as
    displayed lore (text only, option B) or as one bounded mechanical effect
    from ffl/effects.py that the commissioner chooses and applies (option C).
  * The model therefore never has a write path to game mechanics; a human is
    always the executor of anything that changes the database's game state.

Nothing here is wired into the live tick loop yet. The functions are built so a
future tick step can call `maybe_propose`, `cast_missing_votes`, and
`close_if_due` once per tick -- but that wiring is a separate, reviewed change.

Tally rule: among votes actually cast, a bylaw passes iff YES > NO AND at least
GOV_QUORUM yes/no votes were cast. A tie (including 0-0) fails -- the status quo
wins. Abstentions and non-votes count toward neither side.

Published-text safety: proposal/vote text is model-authored and lands in the
public chat feed, so every generation prompt carries llm.VOICE's hard limits and
every stored string is run through effects.sanitize (control-char strip, length
cap).
"""
from __future__ import annotations

import json
import random as _random
from datetime import datetime, timedelta, timezone

from . import config, llm, effects


# --- time / small helpers ---------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _team(conn, tid):
    return conn.execute("SELECT * FROM teams WHERE team_id=?", (tid,)).fetchone()


def _log(conn, team_id, message):
    conn.execute("INSERT INTO chat_log(team_id, event_type, message) "
                 "VALUES(?, 'bylaw', ?)", (team_id, message))
    conn.commit()


def active_voting(conn):
    """Bylaws currently open for votes (at most one at a time by design)."""
    return conn.execute("SELECT * FROM bylaws WHERE status='voting'").fetchall()


# --- proposing --------------------------------------------------------------

def _wants_to_propose(team) -> bool:
    """Haiku gate: proposing is a big, rare move -- most GMs almost never do it."""
    system = (f"You are {team['gm_name']}, a fantasy football GM. Persona: "
              f"{team['personality']} Bio: {team['bio'] or ''} Answer only JSON.")
    user = ("Proposing a new league BYLAW or punishment for everyone to vote on "
            "is a big, rare move -- most GMs almost never bother; only a real "
            "rules-lawyer or a pot-stirrer does it, and only when they genuinely "
            "have an axe to grind right now. Given your persona, do you want to "
            'propose one at this moment? Return {"act": true or false}.')
    return llm.gate(system, user)


def propose(conn, team_id, *, now=None) -> dict | None:
    """One GM drafts a free-form bylaw. Returns the new bylaw row, or None.

    Guarded: only one bylaw may be on the floor ('voting') at a time. The
    proposer is recorded as an implicit YES.
    """
    if active_voting(conn):
        return None
    now = now or _now()
    t = _team(conn, team_id)
    others = ", ".join(r["team_name"] for r in conn.execute(
        "SELECT team_name FROM teams WHERE team_id!=? ORDER BY draft_slot",
        (team_id,)))
    system = (f"You are {t['gm_name']}, GM of \"{t['team_name']}\" in a fantasy "
              f"football league. Persona: {t['personality']} Bio: {t['bio'] or ''} "
              "You are proposing a league BYLAW or punishment for the other GMs to "
              "vote on -- a rule change or a penalty aimed at a rival. Be creative "
              "and in character, but keep it to something a group of friends would "
              "actually vote on." + llm.VOICE)
    user = (f"The other teams: {others}.\n\n"
            "Propose ONE bylaw. Give a short title and a one-paragraph pitch "
            'arguing for it, in character. Return JSON '
            '{"title": "<short title>", "pitch": "<one paragraph>"}.')
    try:
        data = llm.chat_json(system, user, max_tokens=500)
    except (ValueError, TypeError):
        return None
    title = effects.sanitize(data.get("title", ""), config.GOV_TITLE_MAX)
    pitch = effects.sanitize(data.get("pitch", ""), config.GOV_TEXT_MAX)
    if not title:
        return None

    close = now + timedelta(hours=config.GOV_VOTING_WINDOW_HOURS)
    cur = conn.execute(
        "INSERT INTO bylaws(proposer_team_id, title, rationale, status, "
        "votes_open_at, votes_close_at) VALUES(?,?,?, 'voting', ?, ?)",
        (team_id, title, pitch, _fmt(now), _fmt(close)))
    bylaw_id = cur.lastrowid
    conn.commit()
    _log(conn, team_id,
         f'[BYLAW #{bylaw_id} PROPOSED] {t["gm_name"]}: "{title}" -- {pitch}')
    _record_vote(conn, bylaw_id, team_id, "yes", "(proposer)")
    return dict(conn.execute("SELECT * FROM bylaws WHERE bylaw_id=?",
                             (bylaw_id,)).fetchone())


_PROPENSITY = {"trash-talker": 1.6, "moderate": 1.0, "quiet": 0.5}


def maybe_propose(conn, *, rng=None, use_gate=True, now=None) -> dict | None:
    """Pick a persona-weighted GM and, past a low probability pre-gate and the
    Haiku gate, let them propose. Built for a future per-tick call; not wired in
    yet. Returns the new bylaw or None (the common case)."""
    rng = rng or _random
    if active_voting(conn):
        return None
    if use_gate and rng.random() >= config.GOV_PROPOSE_PROB:
        return None
    teams = [dict(r) for r in conn.execute("SELECT * FROM teams")]
    if not teams:
        return None
    starter = rng.choices(
        teams, weights=[_PROPENSITY.get(t["chattiness"], 1.0) for t in teams],
        k=1)[0]
    if use_gate and not _wants_to_propose(starter):
        return None
    return propose(conn, starter["team_id"], now=now)


# --- voting -----------------------------------------------------------------

def _record_vote(conn, bylaw_id, team_id, vote, message):
    conn.execute("INSERT OR IGNORE INTO bylaw_votes(bylaw_id, team_id, vote, "
                 "message) VALUES(?,?,?,?)", (bylaw_id, team_id, vote, message))
    conn.commit()


def _voted(conn, bylaw_id) -> set:
    return {r["team_id"] for r in conn.execute(
        "SELECT team_id FROM bylaw_votes WHERE bylaw_id=?", (bylaw_id,))}


def _cast_vote(conn, bylaw, team) -> dict | None:
    system = (f"You are {team['gm_name']}, GM of \"{team['team_name']}\". Persona: "
              f"{team['personality']} Bio: {team['bio'] or ''} You are voting on a "
              "league bylaw -- vote your own interests and character." + llm.VOICE)
    user = (f'Proposed bylaw #{bylaw["bylaw_id"]}: "{bylaw["title"]}"\n'
            f'Pitch: {bylaw["rationale"]}\n\n'
            "Vote yes, no, or abstain, with a one-line in-character reason. "
            'Return JSON {"vote": "yes"|"no"|"abstain", "message": "<one line>"}.')
    try:
        data = llm.chat_json(system, user, max_tokens=300)
    except (ValueError, TypeError):
        return None
    vote = str(data.get("vote", "")).lower().strip()
    if vote not in ("yes", "no", "abstain"):
        vote = "abstain"
    return {"vote": vote, "message": effects.sanitize(data.get("message", ""), 200)}


def cast_missing_votes(conn, bylaw_id, team_ids=None, *, limit=None,
                       rng=None) -> list[dict]:
    """Have not-yet-voted teams cast a vote in character. A tick calls this once
    per firing (with a small `limit`) so votes trickle in over the window;
    called with no limit it fills in everyone still outstanding. No-op unless the
    bylaw is open."""
    b = conn.execute("SELECT * FROM bylaws WHERE bylaw_id=?", (bylaw_id,)).fetchone()
    if b is None or b["status"] != "voting":
        return []
    if team_ids is None:
        team_ids = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]
    done = _voted(conn, bylaw_id)
    remaining = [tid for tid in team_ids if tid not in done]
    if limit is not None:
        (rng or _random).shuffle(remaining)
        remaining = remaining[:max(0, limit)]
    cast = []
    for tid in remaining:
        team = _team(conn, tid)
        res = _cast_vote(conn, b, team)
        if not res:
            continue
        _record_vote(conn, bylaw_id, tid, res["vote"], res["message"])
        _log(conn, tid, f'[BYLAW #{bylaw_id} VOTE] {team["gm_name"]} votes '
                        f'{res["vote"].upper()}: {res["message"]}')
        cast.append({"team_id": tid, **res})
    return cast


# --- tally / close ----------------------------------------------------------

def tally(conn, bylaw_id) -> dict:
    rows = conn.execute("SELECT vote, COUNT(*) n FROM bylaw_votes WHERE bylaw_id=? "
                        "GROUP BY vote", (bylaw_id,)).fetchall()
    c = {r["vote"]: r["n"] for r in rows}
    yes, no, ab = c.get("yes", 0), c.get("no", 0), c.get("abstain", 0)
    cast = yes + no
    quorum_ok = cast >= config.GOV_QUORUM
    passed = quorum_ok and yes > no
    if not quorum_ok:
        reason = f"no quorum ({cast}/{config.GOV_QUORUM} yes-or-no votes)"
    elif yes > no:
        reason = f"passed {yes}-{no}"
    elif yes == no:
        reason = f"tie {yes}-{no} (ties fail)"
    else:
        reason = f"failed {yes}-{no}"
    return {"yes": yes, "no": no, "abstain": ab, "cast": cast,
            "quorum_ok": quorum_ok, "passed": passed, "reason": reason}


def close_if_due(conn, *, now=None) -> list[dict]:
    """Close any open bylaw whose window has elapsed: tally, set status
    (passed_pending / rejected_vote), and log the verdict. Returns the outcomes."""
    now = now or _now()
    due = conn.execute("SELECT * FROM bylaws WHERE status='voting' "
                       "AND votes_close_at <= ?", (_fmt(now),)).fetchall()
    out = []
    for b in due:
        t = tally(conn, b["bylaw_id"])
        status = "passed_pending" if t["passed"] else "rejected_vote"
        conn.execute("UPDATE bylaws SET status=?, tally_json=?, resolved_at=? "
                     "WHERE bylaw_id=?",
                     (status, json.dumps(t), _fmt(now), b["bylaw_id"]))
        conn.commit()
        verdict = ("PASSED -- pending commissioner approval" if t["passed"]
                   else "REJECTED by vote")
        _log(conn, None, f'[BYLAW #{b["bylaw_id"]} {verdict}] "{b["title"]}" '
                         f'({t["reason"]})')
        out.append({"bylaw_id": b["bylaw_id"], "title": b["title"],
                    "status": status, **t})
    return out


# --- enactment (backs scripts/review_bylaws.py) -----------------------------

def list_bylaws(conn, statuses=None) -> list[dict]:
    q, params = "SELECT * FROM bylaws", ()
    if statuses:
        q += f" WHERE status IN ({','.join('?' * len(statuses))})"
        params = tuple(statuses)
    return [dict(r) for r in conn.execute(q + " ORDER BY bylaw_id", params)]


def pending(conn) -> list[dict]:
    return list_bylaws(conn, ["passed_pending"])


def active_lore(conn) -> list[dict]:
    """Enacted, display-only bylaws -- the standing 'rules' for the digest/dash."""
    return list_bylaws(conn, ["enacted_lore"])


def _require_pending(conn, bylaw_id):
    b = conn.execute("SELECT * FROM bylaws WHERE bylaw_id=?", (bylaw_id,)).fetchone()
    if b is None or b["status"] != "passed_pending":
        return None
    return b


def enact_lore(conn, bylaw_id, *, now=None) -> tuple[bool, str]:
    """Option B: make a passed bylaw a standing, display-only league rule."""
    b = _require_pending(conn, bylaw_id)
    if b is None:
        return False, f"bylaw #{bylaw_id} is not pending approval"
    conn.execute("UPDATE bylaws SET status='enacted_lore', enacted_json=?, "
                 "resolved_at=? WHERE bylaw_id=?",
                 (json.dumps({"kind": "lore"}), _fmt(now or _now()), bylaw_id))
    conn.commit()
    _log(conn, None, f'[BYLAW #{bylaw_id} ENACTED as a league rule] "{b["title"]}"')
    return True, f"bylaw #{bylaw_id} enacted as lore"


def enact_effect(conn, bylaw_id, effect_type, team_id, params,
                 *, now=None) -> tuple[bool, str]:
    """Option C: the commissioner attaches ONE bounded effect to a passed bylaw.
    Re-validates through ffl/effects before applying; nothing changes on failure."""
    b = _require_pending(conn, bylaw_id)
    if b is None:
        return False, f"bylaw #{bylaw_id} is not pending approval"
    ok, res = effects.apply_effect(conn, effect_type, team_id, params, bylaw_id)
    if not ok:
        return False, f"effect rejected: {res}"
    conn.execute(
        "UPDATE bylaws SET status='enacted_effect', enacted_json=?, resolved_at=? "
        "WHERE bylaw_id=?",
        (json.dumps({"kind": "effect", "effect_type": effect_type,
                     "team_id": team_id, "params": params, "summary": res}),
         _fmt(now or _now()), bylaw_id))
    conn.commit()
    _log(conn, None, f'[BYLAW #{bylaw_id} ENACTED] "{b["title"]}" -> {res}')
    return True, res


def reject(conn, bylaw_id, reason="", *, now=None) -> tuple[bool, str]:
    b = _require_pending(conn, bylaw_id)
    if b is None:
        return False, f"bylaw #{bylaw_id} is not pending approval"
    conn.execute("UPDATE bylaws SET status='rejected_admin', enacted_json=?, "
                 "resolved_at=? WHERE bylaw_id=?",
                 (json.dumps({"kind": "rejected", "reason": reason}),
                  _fmt(now or _now()), bylaw_id))
    conn.commit()
    _log(conn, None, f'[BYLAW #{bylaw_id} REJECTED by commissioner] "{b["title"]}"')
    return True, f"bylaw #{bylaw_id} rejected"
