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
    return {r["team_id"]: r["team_name"]
            for r in conn.execute("SELECT team_id, team_name FROM teams")}


def _prior_created_at(conn, season, week):
    """created_at of the most recent EARLIER week's recap, to scope 'new' bylaws."""
    row = conn.execute(
        "SELECT created_at FROM weekly_summaries WHERE season=? AND week<? "
        "ORDER BY week DESC LIMIT 1", (season, week)).fetchone()
    return row["created_at"] if row else None


def gather(conn: sqlite3.Connection, week: int, season: int) -> dict:
    """Collect the raw material for one week's recap (pure reads, no model)."""
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

    standings = [f"{i}. {t['team_name']} ({t['wins']}-{t['losses']}-{t['ties']}, "
                 f"{t['points_for']:.0f} PF)"
                 for i, t in enumerate(conn.execute(
                     "SELECT team_name, wins, losses, ties, points_for FROM teams "
                     "ORDER BY wins DESC, points_for DESC"), 1)]

    # Recent group chat (GM banter only -- skip system/bylaw log lines), oldest
    # first, capped so the prompt stays small.
    chat = [f"{r['team_name'] or 'League'}: {r['message']}"
            for r in reversed(conn.execute(
                "SELECT t.team_name, c.message FROM chat_log c "
                "LEFT JOIN teams t ON c.team_id=t.team_id "
                "WHERE c.team_id IS NOT NULL AND c.event_type!='bylaw' "
                "ORDER BY c.chat_id DESC LIMIT 40").fetchall())]

    since = _prior_created_at(conn, season, week)
    bylaw_q = ("SELECT title, status FROM bylaws WHERE resolved_at IS NOT NULL "
               "AND status IN ('passed_pending','enacted_lore','enacted_effect',"
               "'rejected_vote','rejected_admin')")
    params = ()
    if since:
        bylaw_q += " AND resolved_at > ?"
        params = (since,)
    outcome = {"passed_pending": "passed (awaiting the commissioner)",
               "enacted_lore": "passed and enacted as a league rule",
               "enacted_effect": "passed and enacted with a penalty",
               "rejected_vote": "voted down", "rejected_admin": "vetoed"}
    bylaws = [f'"{b["title"]}" -- {outcome.get(b["status"], b["status"])}'
              for b in conn.execute(bylaw_q + " ORDER BY bylaw_id", params)]

    return {"week": week, "results": results, "standings": standings,
            "chat": chat, "bylaws": bylaws}


_SYS = (
    "You are the staff writer for an autonomous AI fantasy football league. You "
    "write a short weekly recap in THREE voices at once, from the same facts:\n"
    "- lively: an ESPN-style recap, readable and fun, a little personality and "
    "drama, references the group-chat beef and any league rule-making, but stays "
    "clean enough for a public page.\n"
    "- neutral: just the facts -- who beat whom, standings movement, what league "
    "business happened. Plain and short, no snark.\n"
    "- roast: the league's own trash-talk voice -- cocky and profane, roasts the "
    "losers and bad decisions hard, like the group chat.\n"
    "Each voice is 2-4 sentences (roast can be punchier). Do not invent facts, "
    "scores, or names beyond what you're given." + llm.VOICE)


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
        f"League week {week} is in the books. Write the three-voice recap.\n\n"
        f"FINAL SCORES:\n" + "\n".join(data["results"]) + "\n\n"
        f"STANDINGS NOW:\n" + "\n".join(data["standings"]) + "\n\n"
        + (("LEAGUE RULE-MAKING THIS WEEK:\n" + "\n".join(data["bylaws"]) + "\n\n")
           if data["bylaws"] else "")
        + (("RECENT GROUP CHAT (for flavor -- quote/reference loosely, don't "
            "transcribe):\n" + "\n".join(data["chat"]) + "\n\n")
           if data["chat"] else "")
        + 'Return JSON {"lively": "...", "neutral": "...", "roast": "..."}.')
    data_out = chat_json(_SYS, user, max_tokens=900)
    lively = _clean(data_out.get("lively", ""))
    neutral = _clean(data_out.get("neutral", ""))
    roast = _clean(data_out.get("roast", ""))
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
               chat_json=None) -> list[int]:
    """Write recaps for every scored week that doesn't have one yet (auto-weekly +
    backfill). Cheap when caught up (a couple of SELECTs, no model call). Never
    raises. Returns the weeks written."""
    season = season or config.SEASON
    written = []
    try:
        weeks = scored_weeks(conn)
    except Exception:  # noqa: BLE001
        return written
    for wk in weeks:
        try:
            if generate(conn, wk, season=season, chat_json=chat_json):
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
