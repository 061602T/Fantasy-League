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

import random as _random
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config, effects, llm, rosters, worldcontext


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


def _roast_material(conn, exclude_id) -> str:
    """The other GMs and their invented personal quirks -- fair game to roast."""
    rows = conn.execute(
        "SELECT team_id, gm_name, team_name, bio FROM teams WHERE team_id != ?",
        (exclude_id,)).fetchall()
    lines = [f"- {r['gm_name']} ({r['team_name']}): {r['bio']}"
             for r in rows if r["bio"]]
    return "\n".join(lines) if lines else "(no notes on the other GMs)"


def _post(conn, team_id, message):
    conn.execute("INSERT INTO chat_log(team_id, event_type, message) "
                 "VALUES(?, 'banter', ?)", (team_id, message))
    conn.commit()


def _post_at(conn, team_id, message, when, reply_to=None):
    """Post a banter line with an explicit created_at (UTC) and optional
    reply_to link, for the ambient loop's staggered, threaded messages.
    Returns the new chat_id."""
    cur = conn.execute(
        "INSERT INTO chat_log(team_id, event_type, message, reply_to, created_at) "
        "VALUES(?, 'banter', ?, ?, ?)",
        (team_id, message, reply_to, when.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return cur.lastrowid


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
    you_bio = f" About you: {team['bio']}" if team["bio"] else ""
    system = (f"You are {team['gm_name']}, GM of \"{team['team_name']}\", in the "
              f"league group chat. Persona: {team['personality']}{you_bio} "
              f"Chattiness: {team['chattiness']}. Write like a real person in a "
              f"group chat: 1-2 sentences, in character, no narration or "
              f"quotation marks."
              + llm.VOICE)
    user = (f"What just happened: {headline}\n{detail}\n\n"
            f"Your angle: {involvement or 'not directly involved'}\n"
            f"The other GMs (their quirks are fair game to roast):\n"
            f"{_roast_material(conn, team['team_id'])}\n\n"
            f"Recent chat:\n{recent}{worldcontext.prompt_snippet()}\n\n"
            "Post your reaction (gloat, trash-talk, roast someone, make "
            'excuses, joke -- whatever fits you). Return JSON {"message": '
            '"<your post>"}.')
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
    # Governance chat_mute: a muted team can't post (but can still be talked about).
    muted = effects.active_team_ids(conn, "chat_mute", rosters.current_week(conn))
    team_ids = [t for t in team_ids if t not in muted]

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


# --- Ambient chat (the decoupled 15-min loop) ------------------------------

# How likely each chattiness tier is to be picked as a conversation starter.
_CHATTINESS_WEIGHT = {"trash-talker": 3.0, "moderate": 1.4, "quiet": 0.5}


def last_banter_age(conn: sqlite3.Connection) -> float | None:
    """Seconds since the most recent 'banter' post (UTC), or None if there is
    none. Used to enforce a cooldown so the ambient loop doesn't pile onto the
    hourly tick's chat or spam back-to-back firings."""
    row = conn.execute(
        "SELECT created_at FROM chat_log WHERE event_type='banter' "
        "ORDER BY chat_id DESC LIMIT 1").fetchone()
    if not row or not row["created_at"]:
        return None
    try:
        dt = datetime.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0.0, (now - dt).total_seconds())


def _wants_to_chat(team, recent) -> bool:
    """Haiku gate for ambient chat (no event), weighted by chattiness."""
    system = (f"You are {team['gm_name']}, a fantasy football GM whose chattiness "
              f"is '{team['chattiness']}' (quiet = rarely posts, trash-talker = "
              f"posts a lot). Answer only JSON.")
    user = (f"It's a random moment in the league group chat. Recent chat:\n{recent}"
            "\n\nDo you feel like posting something right now -- a reply, a jab, a "
            "hot take, a random thought? Only if it fits your chattiness and "
            'there\'s something worth saying. Return {"act": true or false}.')
    return llm.gate(system, user)


def _ambient_line(conn, team, recent, mode) -> str | None:
    """Sonnet: one ambient group-chat line. mode 'reply' reacts to the recent
    chat; 'fresh' opens a new topic."""
    you_bio = f" About you: {team['bio']}" if team["bio"] else ""
    system = (f"You are {team['gm_name']}, GM of \"{team['team_name']}\", in the "
              f"league group chat. Persona: {team['personality']}{you_bio} "
              f"Chattiness: {team['chattiness']}. Write ONE short line like a real "
              f"person in a group chat -- no narration, no quotation marks."
              + llm.VOICE)
    world = worldcontext.prompt_snippet()
    if mode == "reply":
        user = (f"The league group chat, most recent last:\n{recent}{world}\n\n"
                "Reply to what was just said. React to the SPECIFIC thing they "
                "said -- fire back, pile on, or clown it -- so it reads as a real "
                "back-and-forth, not a new topic. One line. "
                'Return JSON {"message": "<your post>"}.')
    else:
        user = (f"The chat's been quiet. Recent chat (may be stale):\n{recent}\n\n"
                f"The other GMs (fair game to poke):\n"
                f"{_roast_material(conn, team['team_id'])}{world}\n\n"
                "Open something new -- a hot take, a brag, a shot at a rival, a "
                "gripe about your own team, a random football thought. NOT a reply "
                'to anything specific. One line. Return JSON {"message": "<post>"}.')
    try:
        msg = str(llm.chat_json(system, user, max_tokens=400).get("message", "")).strip()
    except (ValueError, TypeError):
        return None
    return msg or None


def ambient_exchange(conn: sqlite3.Connection, *, rng=None, use_gate: bool = True,
                     now=None, max_replies: int = 2) -> list[dict]:
    """Produce a short, threaded ambient exchange (0 to a few messages).

    A starter GM (weighted by chattiness) is gated (Haiku); if they speak they
    either reply to recent chat or open a fresh topic, then up to `max_replies`
    other GMs may reply in turn -- each gated, each seeing the latest messages so
    they reference what was just said. Every message gets its own timestamp,
    staggered by seconds-to-minutes so it reads as it happened over time.
    Returns the posted messages (each with a 'ts' datetime).
    """
    rng = rng or _random
    now = now or datetime.now(timezone.utc)
    # Governance chat_mute: muted teams are pulled from the starter/replier pool.
    muted = effects.active_team_ids(conn, "chat_mute", rosters.current_week(conn))
    teams = [dict(r) for r in conn.execute("SELECT * FROM teams")
             if r["team_id"] not in muted]
    if not teams:
        return []

    starter = rng.choices(
        teams,
        weights=[_CHATTINESS_WEIGHT.get(t["chattiness"], 1.0) for t in teams],
        k=1)[0]
    recent = recent_chat(conn)
    if use_gate and not _wants_to_chat(starter, recent):
        return []

    has_recent = recent != "(quiet so far)"
    mode = "reply" if (has_recent and rng.random() < config.CHAT_TICK_THREAD_PROB) \
        else "fresh"
    msg = _ambient_line(conn, starter, recent, mode)
    if not msg:
        return []

    posted = []
    when = now
    # `last_id` is the message a reply threads onto -- the starter, then each
    # subsequent reply chains onto the one before it.
    last_id = _post_at(conn, starter["team_id"], msg, when)
    posted.append({"team_id": starter["team_id"], "gm_name": starter["gm_name"],
                   "message": msg, "ts": when, "mode": mode, "reply_to": None})

    n_replies = rng.choices([0, 1, 2], weights=[0.35, 0.45, 0.20], k=1)[0]
    n_replies = min(n_replies, max_replies)
    others = [t for t in teams if t["team_id"] != starter["team_id"]]
    rng.shuffle(others)
    for team in others[:n_replies]:
        recent = recent_chat(conn)              # now includes the latest message
        if use_gate and not _wants_to_chat(team, recent):
            continue
        reply = _ambient_line(conn, team, recent, "reply")
        if not reply:
            continue
        when = when + timedelta(seconds=rng.randint(5, 150))
        rid = _post_at(conn, team["team_id"], reply, when, reply_to=last_id)
        posted.append({"team_id": team["team_id"], "gm_name": team["gm_name"],
                       "message": reply, "ts": when, "mode": "reply",
                       "reply_to": last_id})
        last_id = rid
    return posted
