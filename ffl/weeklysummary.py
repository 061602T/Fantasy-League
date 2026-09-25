"""Auto-written weekly recaps in three voices.

When a league week is scored, one Sonnet call turns that week's box scores,
standings, group-chat vibe, and any bylaw business into a short recap written
three ways -- a readable "lively" recap, a "neutral" just-the-facts version, and
a "roast" in the league's trash-talk voice. All three are stored in
``weekly_summaries`` (one row per season+week) and shown on the dashboard's
Weekly Recaps tab, where the reader flips between voices.

Design notes:
  * ONE model call per week returns all three voices, so the facts stay
    consistent across them and the cost is a single call per week.
  * The recap is public (it lands on GitHub Pages), so the prompt carries the
    same HARD LIMITS as llm.VOICE -- the "roast" voice is profane trash talk
    like the chat, never slurs / hate / real protected-trait attacks.
  * Idempotent: a week that already has a row is skipped unless force=True, so
    ``ensure_all`` is safe to call every tick and quietly backfills past weeks.
  * Never raises from ``ensure_all`` -- recap writing must not crash a tick.
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import config, llm


def _clean(s, maxlen: int = 1600) -> str:
    """Strip control chars (keeping newlines/tabs), tidy whitespace, cap length."""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(s or ""))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:maxlen].strip()


def _names(conn) -> dict:
    """team_id -> GM (persona) name. The recap identifies managers by their own
    name, not the team name, so this is what every line below maps through."""
    return {r["team_id"]: r["gm_name"]
            for r in conn.execute("SELECT team_id, gm_name FROM teams")}


def _player_names(conn, ids) -> dict:
    """player_id -> display name, for the players moving in trades/waivers."""
    ids = [i for i in dict.fromkeys(ids) if i]  # dedup, drop falsy
    if not ids:
        return {}
    q = ("SELECT player_id, name FROM players WHERE player_id IN (%s)"
         % ",".join("?" * len(ids)))
    return {r["player_id"]: r["name"] for r in conn.execute(q, ids)}


def _week_window(conn, season, week) -> tuple:
    """The [start, end) timestamps that bound one league week's story, anchored
    to when weeks were actually SCORED -- not when recaps were written (those can
    all share a timestamp if recaps were backfilled in one pass).

    start = when this week was scored (earliest player_weekly_scores.computed_at
    for the week); end = when the NEXT scored week was scored (None if this is
    the latest). Everything the GMs said and did in that window -- reactions,
    trades, waiver moves, bylaw votes -- is this week's material. A week that
    isn't scored yet returns (None, None), i.e. no bound.
    """
    row = conn.execute(
        "SELECT MIN(computed_at) c FROM player_weekly_scores "
        "WHERE season=? AND week=?", (season, week)).fetchone()
    start = row["c"] if row else None
    end = None
    if start:
        nxt = conn.execute(
            "SELECT MIN(computed_at) c FROM player_weekly_scores "
            "WHERE season=? AND week>?", (season, week)).fetchone()
        end = nxt["c"] if nxt and nxt["c"] else None
    return start, end


def _window_sql(col, start, end) -> tuple:
    """A ' AND <col> >= ? AND <col> < ?' fragment (+ params) for [start, end)."""
    frag, params = "", []
    if start:
        frag += f" AND {col} >= ?"
        params.append(start)
    if end:
        frag += f" AND {col} < ?"
        params.append(end)
    return frag, params


def gather(conn: sqlite3.Connection, week: int, season: int) -> dict:
    """Collect the raw material for one week's recap (pure reads, no model).

    The story is the GMs and their decisions, so alongside scores/standings this
    pulls the group chat, the trades and waiver moves they made, and the bylaws
    they proposed and voted on -- all keyed to GM names.
    """
    names = _names(conn)
    results = []
    for m in conn.execute(
            "SELECT home_team_id, away_team_id, home_points, away_points, "
            "winner_team_id FROM matchups WHERE week=? AND status='final' "
            "ORDER BY matchup_id", (week,)):
        hi, lo = max(m["home_points"], m["away_points"]), \
                 min(m["home_points"], m["away_points"])
        if m["winner_team_id"] is None:
            results.append(f"{names.get(m['home_team_id'],'?')} tied "
                           f"{names.get(m['away_team_id'],'?')} {hi:.1f}-{lo:.1f}")
        else:
            w = m["winner_team_id"]
            loser = (m["away_team_id"] if w == m["home_team_id"]
                     else m["home_team_id"])
            results.append(f"{names.get(w,'?')} def. {names.get(loser,'?')} "
                           f"{hi:.1f}-{lo:.1f}")

    standings = [f"{i}. {names.get(t['team_id'],'?')} "
                 f"({t['wins']}-{t['losses']}-{t['ties']}, {t['points_for']:.0f} PF)"
                 for i, t in enumerate(conn.execute(
                     "SELECT team_id, wins, losses, ties, points_for FROM teams "
                     "ORDER BY wins DESC, points_for DESC"), 1)]

    start, end = _week_window(conn, season, week)

    # Group chat from THIS WEEK's window only (GM banter -- skip system/bylaw log
    # lines), oldest first, keyed by GM name. This is the heart of the recap.
    cfrag, cps = _window_sql("c.created_at", start, end)
    chat = [f"{names.get(r['team_id']) or 'League'}: {r['message']}"
            for r in reversed(conn.execute(
                "SELECT c.team_id, c.message FROM chat_log c "
                "WHERE c.team_id IS NOT NULL AND c.event_type!='bylaw'" + cfrag
                + " ORDER BY c.chat_id DESC LIMIT 60", cps).fetchall())]

    # Trades the GMs agreed to during this week's window -- a real decision each made.
    trades = []
    tfrag, tps = _window_sql("resolved_at", start, end)
    tq = ("SELECT from_team_id, to_team_id, details_json FROM transactions "
          "WHERE type='trade' AND status='accepted'" + tfrag + " ORDER BY txn_id")
    for tx in conn.execute(tq, tps):
        try:
            d = json.loads(tx["details_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        pmap = _player_names(conn, (d.get("a_gives") or []) + (d.get("b_gives") or []))
        a_side = ", ".join(pmap.get(p, "?") for p in (d.get("a_gives") or [])) or "cash"
        b_side = ", ".join(pmap.get(p, "?") for p in (d.get("b_gives") or [])) or "cash"
        if d.get("a_faab"):
            a_side += f" + ${d['a_faab']} FAAB"
        if d.get("b_faab"):
            b_side += f" + ${d['b_faab']} FAAB"
        trades.append(f"{names.get(tx['from_team_id'],'?')} sent {a_side} to "
                      f"{names.get(tx['to_team_id'],'?')} for {b_side}")

    # Waiver claims that landed this week (waiver details carry their own week).
    waivers = []
    for tx in conn.execute(
            "SELECT to_team_id, faab_bid, details_json FROM transactions "
            "WHERE type='waiver_claim' AND status='processed' ORDER BY txn_id"):
        try:
            d = json.loads(tx["details_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        if d.get("week") != week:
            continue
        pm = _player_names(conn, [d.get("add"), d.get("drop")])
        line = f"{names.get(tx['to_team_id'],'?')} won {pm.get(d.get('add'),'a free agent')} on waivers"
        if tx["faab_bid"]:
            line += f" for ${tx['faab_bid']}"
        if d.get("drop"):
            line += f" (dropped {pm.get(d.get('drop'),'a player')})"
        waivers.append(line)

    # Bylaws resolved during this week's window -- who proposed each, the vote,
    # the outcome.
    bfrag, params = _window_sql("resolved_at", start, end)
    bylaw_q = ("SELECT proposer_team_id, title, status, tally_json FROM bylaws "
               "WHERE resolved_at IS NOT NULL AND status IN ('passed_pending',"
               "'enacted_lore','enacted_effect','rejected_vote','rejected_admin')"
               + bfrag)
    outcome = {"passed_pending": "passed (awaiting the commissioner)",
               "enacted_lore": "passed and enacted as a league rule",
               "enacted_effect": "passed and enacted with a penalty",
               "rejected_vote": "voted down", "rejected_admin": "vetoed"}
    bylaws = []
    for b in conn.execute(bylaw_q + " ORDER BY bylaw_id", params):
        prop = names.get(b["proposer_team_id"], "someone")
        tally = ""
        try:
            t = json.loads(b["tally_json"] or "{}")
            if t.get("yes") is not None or t.get("no") is not None:
                tally = f" (voted {t.get('yes',0)}-{t.get('no',0)})"
        except Exception:  # noqa: BLE001
            pass
        bylaws.append(f'{prop} proposed "{b["title"]}"{tally} -- '
                      f'{outcome.get(b["status"], b["status"])}')

    return {"week": week, "results": results, "standings": standings,
            "chat": chat, "trades": trades, "waivers": waivers, "bylaws": bylaws}


_SYS = (
    "You are the staff writer for an autonomous AI fantasy football league where "
    "every team is run by an AI general manager (GM) with a distinct personality. "
    "ALWAYS name managers by their GM NAME (the names in the facts, e.g. the name "
    "before each chat line) -- never by team name. Write the week's recap in THREE "
    "voices from the same facts.\n\n"
    "The story is the PEOPLE and their DECISIONS, not the box score. Lead with what "
    "the GMs argued about in the group chat, the trades and waiver moves they chose "
    "to make, and the bylaws they proposed and voted on. Scores and standings are "
    "the backdrop -- work them in, but spend most of the recap on personalities and "
    "decisions. Name names, channel the chat's attitude (paraphrase, never "
    "transcribe verbatim), call back to who said what, and tie decisions to results "
    "where you can: a trade that backfired, a waiver pickup that paid off, a GM who "
    "talked big in chat and then got blown out.\n\n"
    "- lively: the MAIN recap and the long one -- an ESPN-columnist feature of "
    "several full paragraphs. Rich with the group-chat drama and the week's "
    "decisions, lots of personality, but clean enough for a public web page.\n"
    "- neutral: a straightforward account -- who beat whom, standings movement, and "
    "which trades, waiver claims, and bylaws happened and which GM was behind each. "
    "A short paragraph, plain, no snark.\n"
    "- roast: the league's own trash-talk voice -- cocky and profane, roasts the "
    "losers, the bad trades, and the GMs who ran their mouth in chat and then lost, "
    "all by name. Punchy, but give it a solid paragraph.\n\n"
    "Do not invent facts, scores, names, trades, or votes beyond what you're given. "
    "If a section (chat, trades, waivers, bylaws) is absent, simply don't mention "
    "it -- do not make something up to fill it." + llm.VOICE)


def generate(conn: sqlite3.Connection, week: int, *, season: int = None,
             chat_json=None, force: bool = False) -> bool:
    """Write and store the three-voice recap for one week. Returns True if a row
    was written, False if skipped (already present, or no results yet). Raises
    only on a genuine model/DB error -- callers that must not crash use
    ``ensure_all``."""
    season = season or config.SEASON
    if not force and conn.execute(
            "SELECT 1 FROM weekly_summaries WHERE season=? AND week=?",
            (season, week)).fetchone():
        return False
    data = gather(conn, week, season)
    if not data["results"]:
        return False   # week isn't scored yet; nothing to recap

    chat_json = chat_json or llm.chat_json
    user = (
        f"League week {week} is in the books. Write the three-voice recap, keeping "
        f"the focus on the GMs, the group chat, and the decisions they made.\n\n"
        f"FINAL SCORES:\n" + "\n".join(data["results"]) + "\n\n"
        f"STANDINGS NOW:\n" + "\n".join(data["standings"]) + "\n\n"
        + (("TRADES THE GMs MADE:\n" + "\n".join(data["trades"]) + "\n\n")
           if data["trades"] else "")
        + (("WAIVER MOVES:\n" + "\n".join(data["waivers"]) + "\n\n")
           if data["waivers"] else "")
        + (("LEAGUE RULE-MAKING (who proposed, the vote, the outcome):\n"
            + "\n".join(data["bylaws"]) + "\n\n") if data["bylaws"] else "")
        + (("GROUP CHAT (the week's beef -- reference and paraphrase, don't "
            "transcribe; each line is 'GM name: message'):\n"
            + "\n".join(data["chat"]) + "\n\n") if data["chat"] else "")
        + 'Return JSON {"lively": "...", "neutral": "...", "roast": "..."}.')
    data_out = chat_json(_SYS, user, max_tokens=2200)
    lively = _clean(data_out.get("lively", ""), maxlen=4000)
    neutral = _clean(data_out.get("neutral", ""), maxlen=2000)
    roast = _clean(data_out.get("roast", ""), maxlen=2500)
    if not (lively or neutral or roast):
        return False
    conn.execute(
        "INSERT INTO weekly_summaries(season, week, lively, neutral, roast) "
        "VALUES(?,?,?,?,?) ON CONFLICT(season, week) DO UPDATE SET "
        "lively=excluded.lively, neutral=excluded.neutral, roast=excluded.roast",
        (season, week, lively, neutral, roast))
    conn.commit()
    return True


def scored_weeks(conn) -> list[int]:
    """Weeks that have at least one final matchup (matchups are single-season)."""
    return [r["week"] for r in conn.execute(
        "SELECT DISTINCT week FROM matchups WHERE status='final' ORDER BY week")]


def ensure_all(conn: sqlite3.Connection, *, season: int = None,
               chat_json=None, force: bool = False) -> list[int]:
    """Write recaps for every scored week that doesn't have one yet (auto-weekly +
    backfill). Cheap when caught up (a couple of SELECTs, no model call). Never
    raises. Returns the weeks written.

    With ``force=True`` every scored week is REWRITTEN (one model call each),
    overwriting the stored recaps -- used to re-render past weeks in a new style.
    The default (force=False) is what a tick calls, so idle ticks stay free."""
    season = season or config.SEASON
    written = []
    try:
        weeks = scored_weeks(conn)
    except Exception:  # noqa: BLE001
        return written
    for wk in weeks:
        try:
            if generate(conn, wk, season=season, chat_json=chat_json, force=force):
                written.append(wk)
        except Exception:  # noqa: BLE001 -- one bad week must not block the rest
            continue
    return written


def week_summaries(conn: sqlite3.Connection, season: int = None) -> list[dict]:
    """All stored recaps, newest week first, for the dashboard."""
    season = season or config.SEASON
    return [dict(r) for r in conn.execute(
        "SELECT week, lively, neutral, roast, created_at FROM weekly_summaries "
        "WHERE season=? ORDER BY week DESC", (season,))]
