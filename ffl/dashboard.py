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

from . import config, playoffs, scoreproj, winprob

DEFAULT_PATH = os.path.join("~", "ffl-data", "dashboard.html")


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


# --- Data pulls ------------------------------------------------------------

def _league(conn):
    return conn.execute("SELECT * FROM league WHERE id=1").fetchone()


def _standings(conn):
    return conn.execute(
        """SELECT team_name, gm_name, wins, losses, ties, points_for,
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


def _recent_moves(conn, limit=8):
    import json
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT type, status, from_team_id, to_team_id, faab_bid, details_json
             FROM transactions WHERE type IN ('trade','waiver_claim')
            ORDER BY txn_id DESC LIMIT ?""", (limit,)).fetchall()

    parsed, pids = [], set()
    for r in rows:
        try:
            d = json.loads(r["details_json"]) if r["details_json"] else {}
        except ValueError:
            d = {}
        parsed.append((r, d))
        if r["type"] == "waiver_claim":
            pids.update([d.get("add"), d.get("drop")])
        else:
            pids.update(d.get("a_gives", []) + d.get("b_gives", []))
    pids = {p for p in pids if p}
    pname = {}
    if pids:
        marks = ",".join("?" * len(pids))
        pname = {x["player_id"]: x["name"] for x in conn.execute(
            f"SELECT player_id, name FROM players WHERE player_id IN ({marks})",
            list(pids))}

    def nm(pid):
        return pname.get(pid, pid or "?")

    out = []
    for r, d in parsed:
        ok = r["status"] == "processed"
        if r["type"] == "waiver_claim":
            out.append({"kind": "waiver", "ok": ok,
                        "team": names.get(r["to_team_id"], "?"),
                        "add": nm(d.get("add")), "drop": nm(d.get("drop")),
                        "faab": r["faab_bid"]})
        else:
            a = names.get(d.get("a"), names.get(r["from_team_id"], "?"))
            b = names.get(d.get("b"), names.get(r["to_team_id"], "?"))
            a_gets = [nm(x) for x in d.get("b_gives", [])]  # a receives b's players
            b_gets = [nm(x) for x in d.get("a_gives", [])]
            if d.get("b_faab"):
                a_gets.append(f"${d['b_faab']} FAAB")
            if d.get("a_faab"):
                b_gets.append(f"${d['a_faab']} FAAB")
            out.append({"kind": "trade", "ok": ok, "a": a, "b": b,
                        "a_gets": a_gets, "b_gets": b_gets})
    return out


def _recent_chat(conn, limit=16):
    rows = conn.execute(
        """SELECT c.event_type, c.message, t.gm_name
             FROM chat_log c LEFT JOIN teams t ON t.team_id = c.team_id
            ORDER BY c.chat_id DESC LIMIT ?""", (limit,)).fetchall()
    return list(reversed([{"who": r["gm_name"] or "League",
                           "kind": r["event_type"], "msg": r["message"]}
                          for r in rows]))


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

def _standings_rows(rows):
    out = []
    for i, r in enumerate(rows, 1):
        lead = " leader" if i == 1 else ""
        rec = f"{r['wins']}–{r['losses']}–{r['ties']}"
        out.append(
            f"<tr class='row{lead}'>"
            f"<td class='rank'>{i}</td>"
            f"<td class='team'><span class='tname'>{_esc(r['team_name'])}</span>"
            f"<span class='gm'>{_esc(r['gm_name'])}</span></td>"
            f"<td class='num rec'>{rec}</td>"
            f"<td class='num'>{r['points_for']:.1f}</td>"
            f"<td class='num muted'>{r['points_against']:.1f}</td>"
            f"<td class='num faab'>${r['faab_remaining']}</td>"
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


def _moves_list(moves):
    if not moves:
        return "<p class='empty'>No trades or waiver claims yet.</p>"
    none = "nothing"
    items = []
    for m in moves:
        st = "✓" if m["ok"] else "✗"
        stcl = "ok" if m["ok"] else "no"
        if m["kind"] == "waiver":
            badge = "<span class='badge waiver'>WAIVER</span>"
            head = (f"<div class='mvhead'>{_esc(m['team'])}"
                    f"<span class='st {stcl}'>{st}</span></div>")
            faab = f"${m['faab']}" if m["faab"] is not None else ""
            if m["ok"]:
                body = head + (
                    f"<div class='mvline'><span class='add'>&plus; "
                    f"{_esc(m['add'])}</span>"
                    f"<span class='drop'>&minus; {_esc(m['drop'])}</span>"
                    f"<span class='fa'>{faab}</span></div>")
            else:
                body = head + (f"<div class='mvline muted'>missed on "
                               f"{_esc(m['add'])} · {faab} bid</div>")
        else:
            badge = "<span class='badge trade'>TRADE</span>"
            head = (f"<div class='mvhead'>{_esc(m['a'])} ⇄ {_esc(m['b'])}"
                    f"<span class='st {stcl}'>{st}</span></div>")
            if m["ok"]:
                a_txt = ", ".join(m["a_gets"]) or none
                b_txt = ", ".join(m["b_gets"]) or none
                body = head + (
                    f"<div class='mvline'><b>{_esc(m['a'])}</b> get "
                    f"{_esc(a_txt)}</div>"
                    f"<div class='mvline'><b>{_esc(m['b'])}</b> get "
                    f"{_esc(b_txt)}</div>")
            else:
                body = head + "<div class='mvline muted'>talks fell through, no deal</div>"
        items.append(f"<li>{badge}<div class='mv'>{body}</div></li>")
    return "<ul class='moves'>" + "".join(items) + "</ul>"


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
        # System/event lines (no author) read as centred notes, not speech.
        if who == "League" or kind in ("system", "waiver", "collision", "draft"):
            items.append(f"<li class='sysmsg'><span class='line'>{_esc(msg)}</span></li>")
            continue
        tcls = " trade" if kind == "trade_talk" else ""
        chip = (f"<span class='chip' style='background:hsl({_hue(who)} 72% 40%)'>"
                f"{_esc(_initials(who))}</span>")
        items.append(
            f"<li class='msg{tcls}'>{chip}<div class='body'>"
            f"<span class='who'>{_esc(who)}</span>"
            f"<span class='line'>{_esc(msg)}</span></div></li>")
    return "<ul class='chat'>" + "".join(items) + "</ul>"


_CSS = """
:root{
  --bg:#d8c290; --surface:#f0e2ba; --surface-2:#e4d09c; --ink:#1a1204;
  --muted:#5c4718; --line:#1a1204; --accent:#e2560a; --accent-ink:#1a1204;
  --win:#0f6b0f; --loss:#b21212; --gold:#7a5c00;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0a0700; --surface:#151000; --surface-2:#201800; --ink:#f2dca4;
  --muted:#c19a44; --line:#f2dca4; --accent:#ff7d1a; --accent-ink:#0a0700;
  --win:#54e054; --loss:#ff6060; --gold:#ffcf4a;
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
/* Chat */
.chat{list-style:none;margin:0;padding:2px 0;max-height:600px;overflow-y:auto}
.chat li{border-bottom:1px solid var(--line)}
.chat li:last-child{border-bottom:0}
.msg{display:flex;gap:11px;align-items:flex-start;padding:11px 16px}
.chip{flex:0 0 30px;width:30px;height:30px;color:#fff;border:2px solid var(--line);
  font-family:"Oswald",sans-serif;font-size:12px;font-weight:700;letter-spacing:.3px;
  display:flex;align-items:center;justify-content:center}
.msg .body{display:flex;flex-direction:column;gap:2px;min-width:0}
.msg .who{font-size:12px;font-weight:700;color:var(--ink)}
.msg.trade .who{color:var(--accent)}
.msg .line{font-size:14px;line-height:1.45;overflow-wrap:anywhere}
.sysmsg{padding:9px 16px;text-align:center}
.sysmsg .line{font-size:12.5px;color:var(--muted);font-weight:600}
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
    games = _week_matchups(conn, shown_week, season)
    next_week = _next_week(conn)
    upcoming = _upcoming(conn, next_week, season)
    rosters = _rosters(conn)
    moves = _recent_moves(conn)
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
          <th>PF</th><th>PA</th><th>FAAB</th>
        </tr></thead>
        <tbody>{_standings_rows(standings)}</tbody>
      </table>
    </div>
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

  <div class="cols">
    <section>
      <p class="eyebrow">Recent moves</p>
      <div class="card">{_moves_list(moves)}</div>
    </section>
    <section>
      <p class="eyebrow">League chat</p>
      <div class="card">{_chat_feed(chat)}</div>
    </section>
  </div>

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
