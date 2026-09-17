"""Render the league check-in dashboard as a self-contained HTML file.

The tick loop regenerates this each run; a human opens it in a browser (or the
Pi serves it). Pure rendering -- no API calls. Standings, the latest week's
matchups, every team's roster (starters flagged), recent moves, and the group
chat, in a scoreboard treatment that works in light and dark and down to phone
width.

Path: FFL_DASHBOARD_PATH, default ~/ffl-data/dashboard.html.
"""
from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime, timezone

from . import config, playoffs, playoffodds, scoreproj, winprob

DEFAULT_PATH = os.path.join("~", "ffl-data", "dashboard.html")


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _fmt_ts(s) -> str:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) -> 'Mon D · HH:MM UTC'. Blank on junk."""
    if not s:
        return ""
    try:
        dt = datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _esc(s)
    return dt.strftime("%b ") + str(dt.day) + dt.strftime(" · %H:%M UTC")


# --- Data pulls ------------------------------------------------------------

def _league(conn):
    return conn.execute("SELECT * FROM league WHERE id=1").fetchone()


def _standings(conn):
    return conn.execute(
        """SELECT team_id, team_name, gm_name, wins, losses, ties, points_for,
                  points_against, faab_remaining
             FROM teams ORDER BY wins DESC, points_for DESC""").fetchall()


def _latest_final_week(conn):
    row = conn.execute(
        "SELECT MAX(week) w FROM matchups WHERE status='final'").fetchone()
    return row["w"] if row and row["w"] else None


def _week_matchups(conn, week, season_year=None):
    if not week:
        return []
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT home_team_id, away_team_id, home_points, away_points,
                  winner_team_id, status FROM matchups WHERE week=?
            ORDER BY matchup_id""", (week,)).fetchall()
    # Statistical projected totals (recent-form, bye-aware) shown next to the
    # actual score. teams_on_bye is guarded and returns {} on any data trouble.
    season_year = season_year if season_year is not None else config.SEASON
    bye = scoreproj.teams_on_bye(season_year, week)
    out = []
    for m in rows:
        hp_proj = scoreproj.project_team(
            conn, m["home_team_id"], season_year, week, bye_teams=bye)["proj"]
        ap_proj = scoreproj.project_team(
            conn, m["away_team_id"], season_year, week, bye_teams=bye)["proj"]
        out.append({
            "home": names.get(m["home_team_id"], "?"),
            "away": names.get(m["away_team_id"], "?"),
            "hp": m["home_points"], "ap": m["away_points"],
            "hp_proj": hp_proj, "ap_proj": ap_proj,
            "home_win": m["winner_team_id"] == m["home_team_id"],
            "away_win": m["winner_team_id"] == m["away_team_id"],
            "final": m["status"] == "final",
        })
    return out


def _player_names(conn, pids):
    pids = {p for p in pids if p}
    if not pids:
        return {}
    marks = ",".join("?" * len(pids))
    return {x["player_id"]: x["name"] for x in conn.execute(
        f"SELECT player_id, name FROM players WHERE player_id IN ({marks})",
        list(pids))}


def _recent_trades(conn, limit=8):
    """Recent trades (accepted or fell-through), newest first."""
    import json
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT status, from_team_id, to_team_id, details_json
             FROM transactions WHERE type = 'trade'
            ORDER BY txn_id DESC LIMIT ?""", (limit,)).fetchall()
    parsed = [(r, json.loads(r["details_json"]) if r["details_json"] else {})
              for r in rows]
    pname = _player_names(conn, {p for _, d in parsed
                                 for p in d.get("a_gives", []) + d.get("b_gives", [])})

    def nm(pid):
        return pname.get(pid, pid or "?")

    out = []
    for r, d in parsed:
        a = names.get(d.get("a"), names.get(r["from_team_id"], "?"))
        b = names.get(d.get("b"), names.get(r["to_team_id"], "?"))
        a_gets = [nm(x) for x in d.get("b_gives", [])]  # a receives b's players
        b_gets = [nm(x) for x in d.get("a_gives", [])]
        if d.get("b_faab"):
            a_gets.append(f"${d['b_faab']} FAAB")
        if d.get("a_faab"):
            b_gets.append(f"${d['a_faab']} FAAB")
        out.append({"ok": r["status"] == "processed", "a": a, "b": b,
                    "a_gets": a_gets, "b_gets": b_gets})
    return out


def _waiver_history(conn, limit=24):
    """Waiver claim history, newest first: team, add/drop, won/lost, and week."""
    import json
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT status, to_team_id, faab_bid, details_json
             FROM transactions WHERE type = 'waiver_claim'
            ORDER BY txn_id DESC LIMIT ?""", (limit,)).fetchall()
    parsed = [(r, json.loads(r["details_json"]) if r["details_json"] else {})
              for r in rows]
    pname = _player_names(conn, {p for _, d in parsed
                                 for p in (d.get("add"), d.get("drop"))})

    def nm(pid):
        return pname.get(pid, pid or "?")

    return [{"team": names.get(r["to_team_id"], "?"),
             "add": nm(d.get("add")), "drop": nm(d.get("drop")),
             "faab": r["faab_bid"], "won": r["status"] == "processed",
             "week": d.get("week")}
            for r, d in parsed]


def _free_agents(conn, season_year, week, limit=24):
    """Unrostered players still on the wire, ranked by recent-form projection."""
    rows = conn.execute(
        """SELECT p.player_id, p.name, p.position, p.nfl_team FROM players p
            WHERE NOT EXISTS (SELECT 1 FROM rosters r
                               WHERE r.player_id = p.player_id
                                 AND r.dropped_week IS NULL)""").fetchall()
    fas = [{"name": r["name"], "pos": r["position"], "team": r["nfl_team"],
            "proj": scoreproj.project_player(conn, r["player_id"], season_year, week)}
           for r in rows]
    # Best projection first; players with no scoring history sink to the bottom.
    fas.sort(key=lambda x: (x["proj"] is None, -(x["proj"] or 0.0), x["name"]))
    return fas[:limit]


def _recent_chat(conn, limit=24):
    # Newest first (most recent messages on top). A reply can therefore appear
    # above its parent -- the quoted preview keeps it readable either way.
    rows = conn.execute(
        """SELECT c.chat_id, c.event_type, c.message, c.created_at, c.reply_to,
                  t.gm_name
             FROM chat_log c LEFT JOIN teams t ON t.team_id = c.team_id
            ORDER BY c.chat_id DESC LIMIT ?""", (limit,)).fetchall()
    # Look up the parent of any reply (it may be older than the shown window),
    # so a reply can render a quoted preview of the message it answers.
    parent_ids = {r["reply_to"] for r in rows if r["reply_to"]}
    parents = {}
    if parent_ids:
        marks = ",".join("?" * len(parent_ids))
        for p in conn.execute(
                f"""SELECT c.chat_id, c.message, t.gm_name FROM chat_log c
                     LEFT JOIN teams t ON t.team_id = c.team_id
                    WHERE c.chat_id IN ({marks})""", list(parent_ids)):
            parents[p["chat_id"]] = {"who": p["gm_name"] or "League",
                                     "msg": p["message"]}
    return [{"who": r["gm_name"] or "League", "kind": r["event_type"],
             "msg": r["message"], "ts": r["created_at"],
             "parent": parents.get(r["reply_to"]) if r["reply_to"] else None}
            for r in rows]


_POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DST": 5}


def _rosters(conn):
    """Each team's active roster, grouped by position, starters flagged from the
    most recent week that has a set lineup."""
    row = conn.execute("SELECT MAX(week) w FROM lineups").fetchone()
    lw = row["w"] if row else None
    starters = set()
    if lw is not None:
        starters = {(r["team_id"], r["player_id"]) for r in conn.execute(
            "SELECT team_id, player_id FROM lineups WHERE week=? AND slot!='BENCH'",
            (lw,))}

    out = []
    teams = conn.execute(
        "SELECT team_id, team_name, gm_name, bio, wins, losses, ties FROM teams "
        "ORDER BY draft_slot").fetchall()
    for t in teams:
        players = conn.execute(
            """SELECT p.player_id, p.name, p.position
                 FROM rosters r JOIN players p ON p.player_id = r.player_id
                WHERE r.team_id = ? AND r.dropped_week IS NULL""",
            (t["team_id"],)).fetchall()
        plist = [{"name": p["name"], "pos": p["position"],
                  "starter": (t["team_id"], p["player_id"]) in starters}
                 for p in players]
        plist.sort(key=lambda x: (_POS_ORDER.get(x["pos"], 9),
                                  not x["starter"], x["name"]))
        out.append({"name": t["team_name"], "gm": t["gm_name"],
                    "bio": t["bio"] or "",
                    "rec": f"{t['wins']}–{t['losses']}–{t['ties']}",
                    "players": plist})
    return out


# --- HTML pieces -----------------------------------------------------------

def _odds_cell(pct) -> str:
    """A 'Playoff%' cell, emphasised near-locked (>=99%) and shaded out near 0."""
    if pct is None:
        return ""
    if pct >= 99.5:
        cls, txt = "odds lock", "✓"
    elif pct <= 0.5:
        cls, txt = "odds out", "—"
    else:
        cls, txt = "odds", f"{pct:.0f}%"
    return f"<td class='num {cls}'>{txt}</td>"


def _standings_rows(rows, odds=None):
    out = []
    for i, r in enumerate(rows, 1):
        lead = " leader" if i == 1 else ""
        rec = f"{r['wins']}–{r['losses']}–{r['ties']}"
        odds_cell = ""
        if odds is not None:
            odds_cell = _odds_cell(odds.get(r["team_id"], 0.0) * 100)
        out.append(
            f"<tr class='row{lead}'>"
            f"<td class='rank'>{i}</td>"
            f"<td class='team'><span class='tname'>{_esc(r['team_name'])}</span>"
            f"<span class='gm'>{_esc(r['gm_name'])}</span></td>"
            f"<td class='num rec'>{rec}</td>"
            f"<td class='num'>{r['points_for']:.1f}</td>"
            f"<td class='num muted'>{r['points_against']:.1f}</td>"
            f"<td class='num faab'>${r['faab_remaining']}</td>"
            f"{odds_cell}"
            f"</tr>")
    return "\n".join(out)


def _next_week(conn):
    """The next regular-season week that still has an unplayed matchup, if any."""
    row = conn.execute(
        "SELECT MIN(week) w FROM matchups WHERE status != 'final' AND week <= ?",
        (config.REGULAR_SEASON_WEEKS,)).fetchone()
    return row["w"] if row and row["w"] else None


def _upcoming(conn, week, season_year):
    """Next week's matchups with win probability (winprob) and projected totals
    (scoreproj) for each side."""
    if not week:
        return []
    bye = scoreproj.teams_on_bye(season_year, week)
    out = []
    for r in winprob.matchup_winprobs(conn, week):
        hp = scoreproj.project_team(conn, r["home_team_id"], season_year, week,
                                    bye_teams=bye)["proj"]
        ap = scoreproj.project_team(conn, r["away_team_id"], season_year, week,
                                    bye_teams=bye)["proj"]
        out.append({**r, "home_proj": hp, "away_proj": ap})
    return out


def _upcoming_cards(games):
    if not games:
        return "<p class='empty'>No upcoming games scheduled.</p>"
    cards = []
    for g in games:
        hpct = round(g["home_wp"] * 100)
        apct = round(g["away_wp"] * 100)
        hfav = " fav" if g["home_wp"] >= g["away_wp"] else ""
        afav = " fav" if g["away_wp"] > g["home_wp"] else ""
        cards.append(
            f"<div class='game'>"
            f"<div class='side{hfav}'><span class='sname'>{_esc(g['home'])}</span>"
            f"<span class='sbox'><span class='swp'>{hpct}%</span>"
            f"{_proj_tag(g.get('home_proj'))}</span></div>"
            f"<div class='vs'>vs</div>"
            f"<div class='side{afav}'><span class='sname'>{_esc(g['away'])}</span>"
            f"<span class='sbox'><span class='swp'>{apct}%</span>"
            f"{_proj_tag(g.get('away_proj'))}</span></div>"
            f"</div>")
    return "<div class='games'>" + "".join(cards) + "</div>"


def _upcoming_section(games, week) -> str:
    """The whole 'Upcoming' section, or empty when the season has no next week."""
    if not games:
        return ""
    return (
        f'\n  <section>\n'
        f'    <p class="eyebrow">Upcoming — Week {week}</p>\n'
        f'    <p class="mnote">Win % is a statistical estimate from each team’s '
        f'scoring so far (mean &amp; variance, normal-approximation) — not a lock. '
        f'“proj” is the recent-form points estimate.</p>\n'
        f'    {_upcoming_cards(games)}\n'
        f'  </section>')


def _proj_tag(proj) -> str:
    """Small 'proj N.N' label for a team's projected total (blank if unknown)."""
    if proj is None:
        return ""
    return f"<span class='sproj' title='recent-form projection'>proj {proj:.1f}</span>"


def _matchup_cards(games):
    if not games:
        return "<p class='empty'>No games scored yet.</p>"
    cards = []
    for g in games:
        if g["final"]:
            hp = f"{g['hp']:.1f}"; ap = f"{g['ap']:.1f}"
        else:
            hp = ap = "–"
        hcl = " won" if g["home_win"] else ""
        acl = " won" if g["away_win"] else ""
        hpp = _proj_tag(g.get("hp_proj"))
        app = _proj_tag(g.get("ap_proj"))
        cards.append(
            f"<div class='game'>"
            f"<div class='side{hcl}'><span class='sname'>{_esc(g['home'])}</span>"
            f"<span class='sbox'><span class='sscore'>{hp}</span>{hpp}</span></div>"
            f"<div class='vs'>vs</div>"
            f"<div class='side{acl}'><span class='sname'>{_esc(g['away'])}</span>"
            f"<span class='sbox'><span class='sscore'>{ap}</span>{app}</span></div>"
            f"</div>")
    return "<div class='games'>" + "".join(cards) + "</div>"


def _trades_list(trades):
    if not trades:
        return "<p class='empty'>No trades yet.</p>"
    none = "nothing"
    items = []
    for m in trades:
        st = "✓" if m["ok"] else "✗"
        stcl = "ok" if m["ok"] else "no"
        badge = "<span class='badge trade'>TRADE</span>"
        head = (f"<div class='mvhead'>{_esc(m['a'])} ⇄ {_esc(m['b'])}"
                f"<span class='st {stcl}'>{st}</span></div>")
        if m["ok"]:
            a_txt = ", ".join(m["a_gets"]) or none
            b_txt = ", ".join(m["b_gets"]) or none
            body = head + (
                f"<div class='mvline'><b>{_esc(m['a'])}</b> get {_esc(a_txt)}</div>"
                f"<div class='mvline'><b>{_esc(m['b'])}</b> get {_esc(b_txt)}</div>")
        else:
            body = head + "<div class='mvline muted'>talks fell through, no deal</div>"
        items.append(f"<li>{badge}<div class='mv'>{body}</div></li>")
    return "<ul class='moves'>" + "".join(items) + "</ul>"


def _waiver_list(claims):
    if not claims:
        return "<p class='empty'>No waiver claims yet.</p>"
    items = []
    for c in claims:
        won = c["won"]
        tag = "<span class='wtag won'>WON</span>" if won \
            else "<span class='wtag lost'>LOST</span>"
        wk = f"<span class='wk'>Wk {c['week']}</span>" if c["week"] else ""
        faab = f"${c['faab']}" if c["faab"] is not None else ""
        if won:
            detail = (f"<span class='add'>&plus; {_esc(c['add'])}</span>"
                      f"<span class='drop'>&minus; {_esc(c['drop'])}</span>")
        else:
            detail = f"<span class='muted'>missed on {_esc(c['add'])}</span>"
        items.append(
            f"<li><div class='wvhead'>{_esc(c['team'])}{wk}{tag}</div>"
            f"<div class='wvline'>{detail}<span class='fa'>{faab}</span></div></li>")
    return "<ul class='wv'>" + "".join(items) + "</ul>"


def _falist(fas):
    if not fas:
        return "<p class='empty'>No free agents available.</p>"
    items = []
    for f in fas:
        proj = f"proj {f['proj']:.1f}" if f["proj"] is not None else ""
        team = f" · {_esc(f['team'])}" if f["team"] else ""
        items.append(
            f"<li><span class='fpos'>{_esc(f['pos'])}</span>"
            f"<span class='fname'>{_esc(f['name'])}{team}</span>"
            f"<span class='fproj'>{proj}</span></li>")
    return "<ul class='falist'>" + "".join(items) + "</ul>"


def _roster_cards(rosters):
    if not rosters:
        return "<p class='empty'>No rosters yet.</p>"
    cards = []
    for t in rosters:
        lis = []
        for p in t["players"]:
            cls = "starter" if p["starter"] else "bench"
            mark = "<span class='mark'>ST</span>" if p["starter"] else ""
            lis.append(f"<li class='{cls}'><span class='pos'>{_esc(p['pos'])}</span>"
                       f"<span class='pl'>{_esc(p['name'])}</span>{mark}</li>")
        bio = ""
        if t["bio"]:
            bio = (f"<details class='biobox'><summary>Bio &amp; baggage</summary>"
                   f"<p class='bio'>{_esc(t['bio'])}</p></details>")
        cards.append(
            f"<div class='rteam card'><div class='rhead'>"
            f"<span class='rname'>{_esc(t['name'])}</span>"
            f"<span class='rgm'>{_esc(t['gm'])} · {t['rec']}</span></div>"
            f"{bio}"
            f"<ul class='rlist'>{''.join(lis)}</ul></div>")
    return "<div class='rosters'>" + "".join(cards) + "</div>"


def _hue(name: str) -> int:
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 360
    return h


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    letters = parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")
    return letters.upper()


def _chat_feed(chat):
    if not chat:
        return "<p class='empty'>The league chat is quiet.</p>"
    items = []
    for c in chat:
        who, msg = c["who"], c["msg"]
        kind = c["kind"] or ""
        ts = _fmt_ts(c.get("ts"))
        # System/event lines (no author) read as centred notes, not speech.
        if who == "League" or kind in ("system", "waiver", "collision", "draft"):
            when = f"<span class='systs'>{ts}</span>" if ts else ""
            items.append(f"<li class='sysmsg'><span class='line'>{_esc(msg)}</span>"
                         f"{when}</li>")
            continue
        tcls = " trade" if kind == "trade_talk" else ""
        chip = (f"<span class='chip' style='background:hsl({_hue(who)} 72% 40%)'>"
                f"{_esc(_initials(who))}</span>")
        quote = ""
        p = c.get("parent")
        if p:
            snip = p["msg"]
            if len(snip) > 90:
                snip = snip[:90].rstrip() + "…"
            quote = (f"<div class='tquote'><span class='qwho'>↩ {_esc(p['who'])}</span>"
                     f"<span class='qmsg'>{_esc(snip)}</span></div>")
        items.append(
            f"<li class='msg{tcls}'>{chip}<div class='bubble'>"
            f"<div class='byline'><span class='who'>{_esc(who)}</span>"
            f"<span class='ts'>{ts}</span></div>"
            f"{quote}"
            f"<span class='line'>{_esc(msg)}</span></div></li>")
    return "<ul class='chat'>" + "".join(items) + "</ul>"


_CSS = """
:root{
  --bg:#d8c290; --surface:#f0e2ba; --surface-2:#e4d09c; --ink:#1a1204;
  --muted:#5c4718; --line:#1a1204; --accent:#e2560a; --accent-ink:#1a1204;
  --win:#0f6b0f; --loss:#b21212; --gold:#7a5c00;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#2b1f11; --surface:#372817; --surface-2:#44331d; --ink:#f5e2b0;
  --muted:#cbaa64; --line:#f5e2b0; --accent:#ff7d1a; --accent-ink:#2b1f11;
  --win:#6fdd6f; --loss:#ff7a7a; --gold:#ffcf4a;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding-block:24px;padding-left:16px;padding-right:16px}
h1,h2,.rank,.sscore,.scorebar b{font-family:"Oswald","IBM Plex Sans",sans-serif}
.num,.rec,.faab,.sscore,.rank{font-variant-numeric:tabular-nums}
.scorebar{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;
  padding:16px 18px;background:var(--surface);border:2px solid var(--line);
  border-left:8px solid var(--accent)}
.scorebar h1{margin:0;font-size:27px;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
.scorebar .meta{color:var(--ink);font-size:14px;display:flex;gap:14px;flex-wrap:wrap;font-weight:500}
.scorebar .meta b{color:var(--ink);font-weight:700}
.champ{flex-basis:100%;margin-top:8px;font-family:"Oswald",sans-serif;font-size:17px;
  font-weight:700;letter-spacing:.4px;color:var(--accent-ink);background:var(--accent);
  border:2px solid var(--line);padding:5px 10px}
section{margin-top:24px}
.eyebrow{font-size:12px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;
  color:var(--accent-ink);background:var(--accent);border:2px solid var(--line);
  display:inline-block;padding:3px 9px;margin:0 0 12px}
.card{background:var(--surface);border:2px solid var(--line)}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:440px}
thead th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink);
  text-align:right;padding:10px 12px;font-weight:700;border-bottom:2px solid var(--line);
  background:var(--surface-2)}
thead th.l{text-align:left}
.row td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line)}
.row:last-child td{border-bottom:0}
.row.leader td{background:var(--surface-2)}
.row .rank{color:var(--muted);font-size:15px;font-weight:700;text-align:left;width:34px}
.row.leader .rank{color:var(--accent)}
.team{text-align:left!important;display:flex;flex-direction:column;line-height:1.25}
.tname{font-weight:700}
.gm{font-size:12px;color:var(--muted)}
.rec{font-weight:700}
.faab{color:var(--accent);font-weight:700}
.odds{font-weight:700}
.odds.lock{color:var(--win)}
.odds.out{color:var(--muted)}
.muted{color:var(--muted)}
.games{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.game{background:var(--surface);border:2px solid var(--line);padding:12px 14px}
.side{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:4px 0}
.side .sname{font-weight:600}
.side .sbox{display:flex;flex-direction:column;align-items:flex-end;line-height:1.05}
.side .sscore{font-size:20px;font-weight:700;color:var(--muted)}
.side .swp{font-size:20px;font-weight:700;color:var(--muted);font-variant-numeric:tabular-nums;
  font-family:"Oswald","IBM Plex Sans",sans-serif}
.side .sproj{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.02em}
.side.won .sname{color:var(--ink);font-weight:700}
.side.won .sscore{color:var(--accent)}
.side.fav .sname{color:var(--ink);font-weight:700}
.side.fav .swp{color:var(--accent)}
.mnote{font-size:11.5px;color:var(--muted);margin:-6px 0 10px;font-weight:600}
.vs{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;
  text-align:center;margin:2px 0;font-weight:700}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:640px){.cols{grid-template-columns:1fr}}
.moves{list-style:none;margin:0;padding:4px 0}
.moves li{display:flex;align-items:flex-start;gap:10px;padding:11px 16px;
  border-bottom:1px solid var(--line)}
.moves li:last-child{border-bottom:0}
.badge{font-size:10px;font-weight:700;letter-spacing:.06em;padding:3px 7px;
  border:2px solid var(--line);flex-shrink:0;margin-top:1px}
.badge.trade{background:var(--accent);color:var(--accent-ink)}
.badge.waiver{background:var(--surface-2);color:var(--ink)}
.mv{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.mvhead{display:flex;justify-content:space-between;align-items:center;gap:8px;
  font-size:14px;font-weight:700}
.mvline{font-size:13px;overflow-wrap:anywhere}
.mvline b{font-weight:700}
.mvline .add{color:var(--win);font-weight:700;margin-right:9px}
.mvline .drop{color:var(--loss);font-weight:700;margin-right:9px}
.mvline .fa{color:var(--accent);font-weight:700}
.st{font-weight:700}.st.ok{color:var(--win)}.st.no{color:var(--loss)}
/* Rosters */
.rosters{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.rteam{padding:0;overflow:hidden}
.rhead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
  padding:11px 14px;border-bottom:2px solid var(--line);background:var(--surface-2)}
.rname{font-weight:700;font-family:"Oswald",sans-serif;letter-spacing:.3px}
.rgm{font-size:12px;color:var(--muted)}
.rlist{list-style:none;margin:0;padding:4px 0}
.rlist li{display:flex;align-items:center;gap:10px;padding:5px 14px;font-size:13.5px}
.rlist .pos{flex:0 0 34px;font-size:10px;font-weight:700;letter-spacing:.05em;
  color:var(--muted);text-transform:uppercase}
.rlist .pl{flex:1;min-width:0}
.rlist li.bench .pl{color:var(--muted)}
.rlist li.starter .pl{font-weight:700}
.rlist .mark{font-size:9px;font-weight:700;color:var(--accent-ink);
  background:var(--accent);border:1px solid var(--line);padding:1px 5px;letter-spacing:.05em}
.biobox{border-bottom:2px solid var(--line);background:var(--surface)}
.biobox>summary{cursor:pointer;list-style:none;padding:7px 14px;font-size:10px;
  font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);
  display:flex;align-items:center;gap:6px;user-select:none}
.biobox>summary::-webkit-details-marker{display:none}
.biobox>summary::before{content:"▸";font-size:11px}
.biobox[open]>summary::before{content:"▾"}
.biobox[open]>summary{color:var(--accent-ink);background:var(--accent)}
.biobox .bio{margin:0;padding:10px 14px 12px;font-size:12.5px;line-height:1.5;
  color:var(--ink);border-top:1px solid var(--line)}
/* Chat -- a prominent, full-width section right under the standings. */
.eyebrow.big{font-size:14px;padding:5px 12px}
.chatwrap .card{border-width:2px}
.chat{list-style:none;margin:0;padding:4px 0;max-height:560px;overflow-y:auto}
.chat li{border-bottom:1px solid var(--line)}
.chat li:last-child{border-bottom:0}
.msg{display:flex;gap:11px;align-items:flex-start;padding:12px 18px}
.chip{flex:0 0 38px;width:38px;height:38px;color:#fff;border:2px solid var(--line);
  font-family:"Oswald",sans-serif;font-size:15px;font-weight:700;letter-spacing:.3px;
  display:flex;align-items:center;justify-content:center}
/* Each message is a chat bubble; replies carry a quoted preview of their parent. */
.msg .bubble{display:flex;flex-direction:column;gap:4px;min-width:0;max-width:84%;
  background:var(--surface-2);border:2px solid var(--line);padding:8px 12px}
.msg .byline{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.msg .who{font-size:13.5px;font-weight:700;color:var(--ink)}
.msg .ts{font-size:11px;color:var(--muted);font-weight:600;font-variant-numeric:tabular-nums}
.msg.trade .who{color:var(--accent)}
.msg .line{font-size:15.5px;line-height:1.5;overflow-wrap:anywhere}
.msg .tquote{display:flex;flex-direction:column;gap:1px;background:var(--surface);
  border-left:3px solid var(--accent);padding:4px 9px;margin:1px 0 2px}
.msg .qwho{font-size:10.5px;font-weight:700;color:var(--accent);letter-spacing:.02em}
.msg .qmsg{font-size:12px;color:var(--muted);line-height:1.35;overflow-wrap:anywhere;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sysmsg{padding:10px 16px;text-align:center;display:flex;flex-direction:column;gap:2px}
.sysmsg .line{font-size:13px;color:var(--muted);font-weight:600}
.sysmsg .systs{font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums}
/* Waivers */
.subhead{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--ink);margin:0 0 8px}
.wv,.falist{list-style:none;margin:0;padding:4px 0}
.wv li{padding:10px 16px;border-bottom:1px solid var(--line)}
.wv li:last-child,.falist li:last-child{border-bottom:0}
.wvhead{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700}
.wvline{display:flex;align-items:baseline;gap:10px;font-size:13px;margin-top:3px;
  overflow-wrap:anywhere}
.wvline .add{color:var(--win);font-weight:700}
.wvline .drop{color:var(--loss);font-weight:700}
.wvline .fa{color:var(--accent);font-weight:700;margin-left:auto}
.wk{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.04em}
.wtag{font-size:10px;font-weight:700;letter-spacing:.05em;padding:2px 7px;
  border:2px solid var(--line);margin-left:auto}
.wtag.won{background:var(--accent);color:var(--accent-ink)}
.wtag.lost{background:var(--surface-2);color:var(--muted)}
.falist li{display:flex;align-items:baseline;gap:10px;padding:6px 16px;font-size:13.5px;
  border-bottom:1px solid var(--line)}
.falist .fpos{flex:0 0 34px;font-size:10px;font-weight:700;letter-spacing:.05em;
  color:var(--muted);text-transform:uppercase}
.falist .fname{flex:1;min-width:0}
.falist .fproj{color:var(--muted);font-weight:700;font-size:12px;
  font-variant-numeric:tabular-nums}
.empty{color:var(--muted);padding:16px;margin:0}
footer{margin-top:24px;color:var(--muted);font-size:12px;text-align:center;font-weight:600}
"""


def render(conn: sqlite3.Connection) -> str:
    lg = _league(conn)
    season = lg["season"] if lg else config.SEASON
    week = _latest_final_week(conn)
    status = (lg["status"] if lg else "setup").title()
    shown_week = week or (lg["current_week"] if lg else 0) or 1

    standings = _standings(conn)
    # Playoff odds only while regular-season games remain; seed with the count of
    # completed games so identical state gives identical odds tick to tick.
    remaining = playoffodds.remaining_games(conn)
    odds = None
    if remaining:
        n_final = conn.execute(
            "SELECT COUNT(*) FROM matchups WHERE status='final'").fetchone()[0]
        odds = playoffodds.playoff_odds(conn, seed=n_final)
    games = _week_matchups(conn, shown_week, season)
    next_week = _next_week(conn)
    upcoming = _upcoming(conn, next_week, season)
    rosters = _rosters(conn)
    trades = _recent_trades(conn)
    claims = _waiver_history(conn)
    fa_week = next_week or (shown_week + 1)
    free_agents = _free_agents(conn, season, fa_week)
    chat = _recent_chat(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    wk_label = f"Week {week} final" if week else "Preseason"

    champ_id = playoffs.champion(conn)
    champ_html = ""
    if champ_id is not None:
        cn = conn.execute("SELECT team_name FROM teams WHERE team_id=?",
                          (champ_id,)).fetchone()["team_name"]
        champ_html = f'<div class="champ">\U0001f3c6 Champion: {_esc(cn)}</div>'

    reg = config.REGULAR_SEASON_WEEKS
    mlabel = (f"Playoffs — Week {shown_week}" if shown_week > reg
              else f"Week {shown_week} matchups")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Fantasy League</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="scorebar">
    <h1>AI Fantasy League</h1>
    <div class="meta">
      <span>Season <b>{season}</b></span>
      <span><b>{_esc(wk_label)}</b></span>
      <span>Status <b>{_esc(status)}</b></span>
      <span>{len(standings)} teams</span>
    </div>
    {champ_html}
  </div>

  <section>
    <p class="eyebrow">Standings</p>
    <div class="card tablewrap">
      <table>
        <thead><tr>
          <th class="l">#</th><th class="l">Team</th><th>Rec</th>
          <th>PF</th><th>PA</th><th>FAAB</th>{"<th>Playoff%</th>" if odds else ""}
        </tr></thead>
        <tbody>{_standings_rows(standings, odds)}</tbody>
      </table>
    </div>
    {'<p class="mnote">Playoff% = share of 10,000 rest-of-season simulations '
     'in which the team finishes in the top ' + str(playoffs._bracket_size()) +
     ' — a statistical estimate from current standings and each team’s scoring '
     'so far.</p>' if odds else ''}
  </section>

  <section class="chatwrap">
    <p class="eyebrow big">League chat</p>
    <div class="card">{_chat_feed(chat)}</div>
  </section>

  <section>
    <p class="eyebrow">{_esc(mlabel)}</p>
    <p class="mnote">“proj” is a statistical estimate of each team’s total from its
      starters’ recent scoring (last {config.SCORE_PROJ_WINDOW} games, bye-adjusted)
      — not a prediction of the real games.</p>
    {_matchup_cards(games)}
  </section>
{_upcoming_section(upcoming, next_week)}

  <section>
    <p class="eyebrow">Rosters</p>
    {_roster_cards(rosters)}
  </section>

  <section>
    <p class="eyebrow">Waivers</p>
    <div class="cols">
      <div>
        <p class="subhead">Claim history</p>
        <div class="card">{_waiver_list(claims)}</div>
      </div>
      <div>
        <p class="subhead">Available free agents{f' — top {len(free_agents)}' if free_agents else ''}</p>
        <div class="card">{_falist(free_agents)}</div>
      </div>
    </div>
  </section>

  <section>
    <p class="eyebrow">Recent trades</p>
    <div class="card">{_trades_list(trades)}</div>
  </section>

  <footer>Created by Trevor Blum · AI Fantasy Football League ·
    generated {now}</footer>
</div>
</body>
</html>"""


def write(conn: sqlite3.Connection, path: str = None) -> str:
    """Render and write the dashboard to disk. Returns the path."""
    path = path or os.path.expanduser(
        os.environ.get("FFL_DASHBOARD_PATH", DEFAULT_PATH))
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(conn))
    return path
