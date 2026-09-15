"""Event-aware league group chat.

The GMs talk trash and banter in a shared channel, reacting to what just
happened. A cheap Haiku gate decides whether a GM chimes in at all -- weighted
by their `chattiness` (quiet / moderate / trash-talker) and how much the event
involves them -- and Sonnet writes the actual line in character. Messages see
the recent chat, so a couple of rounds produce a threaded back-and-forth.

`react_to_event` is generic: the tick loop can fire it for any event (a trade,
a waiver, a blowout). `react_to_week` builds the event summary from the week's
real matchup results and hands it off.

Everything is written to `chat_log` (event_type 'banter').
"""
from __future__ import annotations

import sqlite3

from . import llm


def _team(conn, tid):
    return conn.execute("SELECT * FROM teams WHERE team_id=?", (tid,)).fetchone()


def recent_chat(conn: sqlite3.Connection, limit: int = 10) -> str:
    """The last few chat lines, oldest-first, as 'GM: message' text."""
    rows = conn.execute(
        """SELECT c.message, t.gm_name FROM chat_log c
             LEFT JOIN teams t ON t.team_id = c.team_id
            ORDER BY c.chat_id DESC LIMIT ?""", (limit,)).fetchall()
    lines = [f"{r['gm_name'] or 'League'}: {r['message']}" for r in reversed(rows)]
    return "\n".join(lines) if lines else "(quiet so far)"


def _post(conn, team_id, message):
    conn.execute("INSERT INTO chat_log(team_id, event_type, message) "
                 "VALUES(?, 'banter', ?)", (team_id, message))
    conn.commit()


def _wants_to_speak(team, headline, involvement, recent) -> bool:
    """Haiku gate, weighted by the GM's chattiness."""
    system = (f"You are {team['gm_name']}, a fantasy football GM whose chattiness "
              f"is '{team['chattiness']}' (quiet = rarely posts, trash-talker = "
              f"posts often). Answer only JSON.")
    user = (f"League group chat. Event: {headline}\n"
            f"Your angle: {involvement or 'not directly involved'}\n"
            f"Recent chat:\n{recent}\n\n"
            "Given your chattiness, do you want to post a message right now? "
            'Return {"act": true or false}.')
    return llm.gate(system, user)


def _compose(conn, team, headline, detail, involvement, recent) -> str | None:
    """Sonnet: write one in-character group-chat line."""
    system = (f"You are {team['gm_name']}, GM of \"{team['team_name']}\", in the "
              f"league group chat. Persona: {team['personality']} Chattiness: "
              f"{team['chattiness']}. Write like a real person in a group chat: "
              f"1-2 sentences, in character, no narration or quotation marks.")
    user = (f"What just happened: {headline}\n{detail}\n\n"
            f"Your angle: {involvement or 'not directly involved'}\n"
            f"Recent chat:\n{recent}\n\n"
            "Post your reaction (gloat, trash-talk, make excuses, joke -- "
            'whatever fits you). Return JSON {"message": "<your post>"}.')
    try:
        msg = str(llm.chat_json(system, user, max_tokens=500).get("message", "")).strip()
    except (ValueError, TypeError):
        return None
    return msg or None


def react_to_event(conn: sqlite3.Connection, headline: str, detail: str = "",
                   involvement: dict[int, str] = None, rounds: int = 2,
                   team_ids=None, use_gate: bool = True) -> list[dict]:
    """Let GMs banter about an event over a few rounds. Returns posted messages.

    `involvement` maps team_id -> a short note on that GM's stake in the event.
    Later rounds see earlier posts, so GMs reply to each other.
    """
    involvement = involvement or {}
    if team_ids is None:
        team_ids = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]

    posted = []
    for _ in range(rounds):
        recent = recent_chat(conn)
        for tid in team_ids:
            team = _team(conn, tid)
            note = involvement.get(tid, "")
            if use_gate and not _wants_to_speak(team, headline, note, recent):
                continue
            msg = _compose(conn, team, headline, detail, note, recent)
            if msg:
                _post(conn, tid, msg)
                posted.append({"team_id": tid, "gm_name": team["gm_name"],
                               "message": msg})
                recent = recent_chat(conn)  # so same-round posts see each other
    return posted


# --- Weekly result reactions -----------------------------------------------

def week_summary(conn: sqlite3.Connection, week: int):
    """Build (headline, detail, involvement) from a scored week's matchups."""
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT home_team_id, away_team_id, home_points, away_points,
                  winner_team_id FROM matchups WHERE week=? AND status='final'""",
        (week,)).fetchall()
    if not rows:
        return None

    lines, scores, involvement = [], {}, {}
    for m in rows:
        h, a = m["home_team_id"], m["away_team_id"]
        hp, ap = m["home_points"], m["away_points"]
        scores[h], scores[a] = hp, ap
        win = m["winner_team_id"]
        margin = abs(hp - ap)
        if win is None:
            lines.append(f"{names[h]} tied {names[a]} {hp:.1f}-{ap:.1f}")
        else:
            lo = a if win == h else h
            lines.append(f"{names[win]} beat {names[lo]} "
                         f"{max(hp, ap):.1f}-{min(hp, ap):.1f} "
                         f"(by {margin:.1f})")
        for tid, mine, opp in ((h, hp, ap), (a, ap, hp)):
            other = a if tid == h else h
            if win is None:
                involvement[tid] = f"you tied {names[other]} {mine:.1f}-{opp:.1f}"
            elif win == tid:
                involvement[tid] = f"you WON {mine:.1f}-{opp:.1f} over {names[other]}"
            else:
                involvement[tid] = f"you LOST {mine:.1f}-{opp:.1f} to {names[other]}"

    hi = max(scores, key=scores.get)
    lo = min(scores, key=scores.get)
    involvement[hi] += " -- and posted the week's HIGH score"
    involvement[lo] += " -- and posted the week's LOW score"
    detail = ("\n".join(lines)
              + f"\nWeek high: {names[hi]} ({scores[hi]:.1f}); "
                f"low: {names[lo]} ({scores[lo]:.1f}).")
    return f"Week {week} is in the books!", detail, involvement


def react_to_week(conn: sqlite3.Connection, week: int, **kw) -> list[dict]:
    summary = week_summary(conn, week)
    if summary is None:
        return []
    headline, detail, involvement = summary
    return react_to_event(conn, headline, detail, involvement, **kw)
