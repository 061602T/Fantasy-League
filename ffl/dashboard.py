"""Render the league check-in dashboard as a self-contained HTML file.

The tick loop regenerates this each run; a human opens it in a browser (or the
Pi serves it). Pure rendering -- no API calls. Standings, the latest week's
matchups, recent roster moves, and the group chat, in a scoreboard treatment
that works in light and dark and down to phone width.

Path: FFL_DASHBOARD_PATH, default ~/ffl-data/dashboard.html.
"""
from __future__ import annotations

import html
import os
import sqlite3
from datetime import datetime, timezone

from . import config, playoffs

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


def _week_matchups(conn, week):
    if not week:
        return []
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT home_team_id, away_team_id, home_points, away_points,
                  winner_team_id, status FROM matchups WHERE week=?
            ORDER BY matchup_id""", (week,)).fetchall()
    out = []
    for m in rows:
        out.append({
            "home": names.get(m["home_team_id"], "?"),
            "away": names.get(m["away_team_id"], "?"),
            "hp": m["home_points"], "ap": m["away_points"],
            "home_win": m["winner_team_id"] == m["home_team_id"],
            "away_win": m["winner_team_id"] == m["away_team_id"],
            "final": m["status"] == "final",
        })
    return out


def _recent_moves(conn, limit=8):
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    rows = conn.execute(
        """SELECT type, status, from_team_id, to_team_id, faab_bid, details_json,
                  COALESCE(resolved_at, created_at) AS ts
             FROM transactions
            WHERE type IN ('trade','waiver_claim')
            ORDER BY txn_id DESC LIMIT ?""", (limit,)).fetchall()
    import json
    out = []
    for r in rows:
        try:
            d = json.loads(r["details_json"]) if r["details_json"] else {}
        except ValueError:
            d = {}
        out.append({"type": r["type"], "status": r["status"],
                    "from": names.get(r["from_team_id"], ""),
                    "to": names.get(r["to_team_id"], ""),
                    "faab": r["faab_bid"], "detail": d})
    return out


def _recent_chat(conn, limit=16):
    rows = conn.execute(
        """SELECT c.event_type, c.message, t.gm_name
             FROM chat_log c LEFT JOIN teams t ON t.team_id = c.team_id
            ORDER BY c.chat_id DESC LIMIT ?""", (limit,)).fetchall()
    return list(reversed([{"who": r["gm_name"] or "League",
                           "kind": r["event_type"], "msg": r["message"]}
                          for r in rows]))


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
        cards.append(
            f"<div class='game'>"
            f"<div class='side{hcl}'><span class='sname'>{_esc(g['home'])}</span>"
            f"<span class='sscore'>{hp}</span></div>"
            f"<div class='vs'>vs</div>"
            f"<div class='side{acl}'><span class='sname'>{_esc(g['away'])}</span>"
            f"<span class='sscore'>{ap}</span></div>"
            f"</div>")
    return "<div class='games'>" + "".join(cards) + "</div>"


def _moves_list(moves):
    if not moves:
        return "<p class='empty'>No trades or waiver claims yet.</p>"
    items = []
    for m in moves:
        badge = "TRADE" if m["type"] == "trade" else "WAIVER"
        bcl = "trade" if m["type"] == "trade" else "waiver"
        ok = m["status"] in ("processed",)
        st = "✓" if ok else "✗"
        stcl = "ok" if ok else "no"
        if m["type"] == "waiver_claim":
            d = m["detail"]
            text = (f"{_esc(m['to'])} — add/drop"
                    + (f" for ${m['faab']}" if m["faab"] is not None else ""))
        else:
            text = f"{_esc(m['from'])} ↔ {_esc(m['to'])}"
        items.append(
            f"<li><span class='badge {bcl}'>{badge}</span>"
            f"<span class='mv'>{text}</span>"
            f"<span class='st {stcl}'>{st}</span></li>")
    return "<ul class='moves'>" + "".join(items) + "</ul>"


def _chat_feed(chat):
    if not chat:
        return "<p class='empty'>The league chat is quiet.</p>"
    items = []
    for c in chat:
        kind = c["kind"] or ""
        cls = {"banter": "banter", "trade_talk": "trade", "waiver": "sys",
               "collision": "sys", "system": "sys", "draft": "sys"}.get(kind, "sys")
        items.append(
            f"<li class='{cls}'><span class='who'>{_esc(c['who'])}</span>"
            f"<span class='line'>{_esc(c['msg'])}</span></li>")
    return "<ul class='chat'>" + "".join(items) + "</ul>"


_CSS = """
:root{
  --bg:#f3f6f2; --surface:#ffffff; --surface-2:#eef2ec; --ink:#15201a;
  --muted:#5e6e63; --line:#e1e7de; --accent:#1c8347; --accent-soft:#e4f2ea;
  --win:#1c8347; --loss:#bd4a30; --gold:#b9862a;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0d120f; --surface:#151b16; --surface-2:#1d241e; --ink:#e7ede8;
  --muted:#8ea093; --line:#27302a; --accent:#43c176; --accent-soft:#17281d;
  --win:#43c176; --loss:#e0785f; --gold:#dfb14e;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding-block:24px;padding-left:16px;padding-right:16px}
h1,h2,.rank,.sscore,.scorebar b{font-family:"Oswald","IBM Plex Sans",sans-serif}
.num,.rec,.faab,.sscore,.rank{font-variant-numeric:tabular-nums}
.scorebar{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;
  padding:18px 20px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;border-left:5px solid var(--accent)}
.scorebar h1{margin:0;font-size:26px;font-weight:700;letter-spacing:.5px;
  text-transform:uppercase}
.scorebar .meta{color:var(--muted);font-size:14px;display:flex;gap:14px;flex-wrap:wrap}
.scorebar .meta b{color:var(--ink);font-weight:600}
.champ{flex-basis:100%;margin-top:6px;font-family:"Oswald",sans-serif;
  font-size:18px;font-weight:600;letter-spacing:.4px;color:var(--gold)}
section{margin-top:26px}
.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--accent);font-weight:600;margin:0 0 10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px}
.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:440px}
thead th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);text-align:right;padding:12px 14px;font-weight:600;
  border-bottom:1px solid var(--line)}
thead th.l{text-align:left}
.row td{padding:11px 14px;text-align:right;border-bottom:1px solid var(--line)}
.row:last-child td{border-bottom:0}
.row .rank{color:var(--muted);font-size:15px;text-align:left;width:34px}
.row.leader .rank{color:var(--gold)}
.team{text-align:left!important;display:flex;flex-direction:column;line-height:1.25}
.tname{font-weight:600}
.gm{font-size:12px;color:var(--muted)}
.rec{font-weight:600}
.faab{color:var(--accent)}
.muted{color:var(--muted)}
.games{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.game{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px}
.side{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:4px 0}
.side .sname{font-weight:500}
.side .sscore{font-size:20px;font-weight:600;color:var(--muted)}
.side.won .sname{color:var(--ink);font-weight:600}
.side.won .sscore{color:var(--win)}
.vs{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;
  text-align:center;margin:2px 0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media (max-width:640px){.cols{grid-template-columns:1fr}}
.moves{list-style:none;margin:0;padding:6px 0}
.moves li{display:flex;align-items:center;gap:10px;padding:9px 16px;
  border-bottom:1px solid var(--line)}
.moves li:last-child{border-bottom:0}
.badge{font-size:10px;font-weight:700;letter-spacing:.06em;padding:3px 7px;
  border-radius:5px;flex-shrink:0}
.badge.trade{background:var(--accent-soft);color:var(--accent)}
.badge.waiver{background:var(--surface-2);color:var(--muted)}
.mv{flex:1;font-size:14px}
.st{font-weight:700}.st.ok{color:var(--win)}.st.no{color:var(--loss)}
.chat{list-style:none;margin:0;padding:6px 0;max-height:520px;overflow-y:auto}
.chat li{padding:8px 16px;border-bottom:1px solid var(--line)}
.chat li:last-child{border-bottom:0}
.chat .who{display:block;font-size:12px;font-weight:600;color:var(--accent)}
.chat li.sys .who{color:var(--muted)}
.chat li.trade .who{color:var(--gold)}
.chat .line{font-size:14px}
.empty{color:var(--muted);padding:16px;margin:0}
footer{margin-top:26px;color:var(--muted);font-size:12px;text-align:center}
"""


def render(conn: sqlite3.Connection) -> str:
    lg = _league(conn)
    season = lg["season"] if lg else config.SEASON
    week = _latest_final_week(conn)
    status = (lg["status"] if lg else "setup").title()
    shown_week = week or (lg["current_week"] if lg else 0) or 1

    standings = _standings(conn)
    games = _week_matchups(conn, shown_week)
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
    {_matchup_cards(games)}
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

  <footer>Generated {now} · AI Fantasy Football League</footer>
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
