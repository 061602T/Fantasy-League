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
import json
import os
import sqlite3
from datetime import datetime, timezone

from . import config, governance, playoffs, playoffodds, scoreproj, winprob

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
        """SELECT matchup_id, home_team_id, away_team_id, home_points, away_points,
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
            "mid": m["matchup_id"], "week": week,
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
    # The draft (pick lines and the reactions posted during it) has its own
    # board, so keep everything up to and including the draft window out of the
    # league chat -- otherwise those rows leak in as system lines.
    cut = conn.execute(
        "SELECT MAX(chat_id) h FROM chat_log WHERE event_type = 'draft'"
    ).fetchone()["h"]
    where = "WHERE c.chat_id > ?" if cut is not None else ""
    params = ([cut] if cut is not None else []) + [limit]
    rows = conn.execute(
        f"""SELECT c.chat_id, c.event_type, c.message, c.created_at, c.reply_to,
                  c.team_id, t.gm_name
             FROM chat_log c LEFT JOIN teams t ON t.team_id = c.team_id
            {where}
            ORDER BY c.chat_id DESC LIMIT ?""", params).fetchall()
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
             "msg": r["message"], "ts": r["created_at"], "team_id": r["team_id"],
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
        game = (
            f"<div class='game'>"
            f"<div class='side{hcl}'><span class='sname'>{_esc(g['home'])}</span>"
            f"<span class='sbox'><span class='sscore'>{hp}</span>{hpp}</span></div>"
            f"<div class='vs'>vs</div>"
            f"<div class='side{acl}'><span class='sname'>{_esc(g['away'])}</span>"
            f"<span class='sbox'><span class='sscore'>{ap}</span>{app}</span></div>"
            f"</div>")
        # A scored game links to its per-player box score (a :target modal).
        if g["final"] and g.get("mid") is not None:
            cards.append(f"<a class='gamelink' href='#box-{g['mid']}' "
                         f"title='View box score'>{game}</a>")
        else:
            cards.append(game)
    return "<div class='games'>" + "".join(cards) + "</div>"


def _weeks_region(conn, season_year, reg) -> str:
    """A section per completed week, newest week first (chronological, down),
    each showing that week's matchups with projected vs actual."""
    weeks = [r["week"] for r in conn.execute(
        "SELECT DISTINCT week FROM matchups WHERE status='final' ORDER BY week DESC")]
    if not weeks:
        return ""
    note = (f'<p class="mnote">“proj” is a recent-form statistical estimate of a '
            f'team’s total (last {config.SCORE_PROJ_WINDOW} games, bye-adjusted) '
            f'shown beside the actual score — not a prediction.</p>')
    out = []
    for i, wk in enumerate(weeks):
        label = f"Playoffs — Week {wk}" if wk > reg else f"Week {wk}"
        games = _week_matchups(conn, wk, season_year)
        out.append(f'  <section>\n    <p class="eyebrow">{_esc(label)}</p>\n'
                   f'    {note if i == 0 else ""}\n'
                   f'    {_matchup_cards(games)}\n  </section>')
    return "\n".join(out)


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
        # Each roster is collapsible and starts collapsed (no `open` attribute).
        cards.append(
            f"<details class='rteam card'><summary class='rhead'>"
            f"<span class='rname'>{_esc(t['name'])}</span>"
            f"<span class='rgm'>{_esc(t['gm'])} · {t['rec']}</span></summary>"
            f"<ul class='rlist'>{''.join(lis)}</ul></details>")
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


def _chat_feed(chat, bios=None):
    if not chat:
        return "<p class='empty'>The league chat is quiet.</p>"
    bios = bios or {}
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
        # The name links to that GM's bio card when they have one.
        tid = c.get("team_id")
        if tid in bios:
            name = (f"<a class='who namelink' href='#bio-{tid}' "
                    f"title='View bio'>{_esc(who)}</a>")
        else:
            name = f"<span class='who'>{_esc(who)}</span>"
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
            f"<div class='byline'>{name}<span class='ts'>{ts}</span></div>"
            f"{quote}"
            f"<span class='line'>{_esc(msg)}</span></div></li>")
    return "<ul class='chat'>" + "".join(items) + "</ul>"


def _draft_chat(conn):
    """The draft, oldest first (R1.1 on top): every pick line plus the rival
    reactions that landed during it. Bounded by the chat_id span of the 'draft'
    rows -- 'banter' is reused by the regular-season chat, so the window, not the
    event_type, is what keeps pre-draft setup notes and in-season chatter out."""
    win = conn.execute(
        "SELECT MIN(chat_id) lo, MAX(chat_id) hi FROM chat_log "
        "WHERE event_type = 'draft'").fetchone()
    if not win or win["lo"] is None:
        return []
    rows = conn.execute(
        """SELECT c.event_type, c.message, c.created_at, c.team_id, t.gm_name
             FROM chat_log c LEFT JOIN teams t ON t.team_id = c.team_id
            WHERE c.chat_id BETWEEN ? AND ?
            ORDER BY c.chat_id ASC""", (win["lo"], win["hi"])).fetchall()
    return [{"who": r["gm_name"] or "League", "kind": r["event_type"],
             "msg": r["message"], "ts": r["created_at"], "team_id": r["team_id"]}
            for r in rows]


def _draft_feed(picks, bios=None):
    """The draft board rendered like the chat: each pick is a bubble from the GM
    who made it (round tag + selection + quip), rival reactions inline beneath."""
    if not picks:
        return "<p class='empty'>No draft on record yet.</p>"
    bios = bios or {}
    items = []
    for c in picks:
        who, msg = c["who"], c["msg"]
        kind = c["kind"] or ""
        chip = (f"<span class='chip' style='background:hsl({_hue(who)} 72% 40%)'>"
                f"{_esc(_initials(who))}</span>")
        tid = c.get("team_id")
        if tid in bios:
            name = (f"<a class='who namelink' href='#bio-{tid}' "
                    f"title='View bio'>{_esc(who)}</a>")
        else:
            name = f"<span class='who'>{_esc(who)}</span>"
        if kind == "draft":
            # Stored as 'R1.1 {who} selects Player (POS) - "quip"'. The round tag
            # and author both go in the byline, so strip that prefix off the line.
            tag = msg.split(" ", 1)[0] if msg[:1] == "R" else ""
            marker = f"{tag} {who} selects "
            body = msg[len(marker):] if tag and msg.startswith(marker) else msg
            meta = f"<span class='dtag'>{_esc(tag)}</span>" if tag else ""
            items.append(
                f"<li class='msg pick'>{chip}<div class='bubble'>"
                f"<div class='byline'>{name}{meta}</div>"
                f"<span class='line'>{_esc(body)}</span></div></li>")
        else:
            items.append(
                f"<li class='msg reax'>{chip}<div class='bubble'>"
                f"<div class='byline'>{name}<span class='ts'>reacts</span></div>"
                f"<span class='line'>{_esc(msg)}</span></div></li>")
    return "<ul class='chat draft'>" + "".join(items) + "</ul>"


_SLOT_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4, "K": 5, "DST": 6}


def _box_score(conn, week, team_id, season):
    """A team's starters for a week with each one's fantasy points, in lineup
    order. Points come from player_weekly_scores; the sum is the team's total."""
    rows = conn.execute(
        """SELECT l.slot, p.name, p.position, s.fantasy_points AS pts
             FROM lineups l
             JOIN players p ON p.player_id = l.player_id
             LEFT JOIN player_weekly_scores s
               ON s.player_id = l.player_id AND s.season = ? AND s.week = ?
            WHERE l.team_id = ? AND l.week = ? AND l.slot != 'BENCH'""",
        (season, week, team_id, week)).fetchall()
    items = [{"slot": r["slot"], "name": r["name"], "pos": r["position"],
              "pts": r["pts"]} for r in rows]
    items.sort(key=lambda x: (_SLOT_ORDER.get(x["slot"], 9), -(x["pts"] or 0.0)))
    return items


def _box_modals(conn, season) -> str:
    """Hidden per-matchup box scores for every scored game, revealed via :target
    when its card is clicked (backdrop / × close). One modal per final matchup."""
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    finals = conn.execute(
        """SELECT matchup_id, week, home_team_id, away_team_id, home_points,
                  away_points, winner_team_id FROM matchups
            WHERE status = 'final' ORDER BY week DESC, matchup_id""").fetchall()

    def _col(tid, pts, won):
        rows = _box_score(conn, m["week"], tid, season)
        lis = "".join(
            f"<li class='boxrow'><span class='bslot'>{_esc(b['slot'])}</span>"
            f"<span class='bname'>{_esc(b['name'])} "
            f"<span class='bpos'>{_esc(b['pos'])}</span></span>"
            f"<span class='bpts'>"
            f"{('%.1f' % b['pts']) if b['pts'] is not None else '&ndash;'}</span></li>"
            for b in rows)
        if not lis:
            lis = ("<li class='boxrow'><span class='bname'>No lineup recorded "
                   "for this week.</span></li>")
        wc = " won" if won else ""
        return (f"<div class='boxcol'><div class='boxteam{wc}'>"
                f"<span>{_esc(names.get(tid, '?'))}</span>"
                f"<span class='boxtot'>{pts:.1f}</span></div>"
                f"<ul class='boxlist'>{lis}</ul></div>")

    mods = []
    for m in finals:
        h, a = m["home_team_id"], m["away_team_id"]
        hp = m["home_points"] if m["home_points"] is not None else 0.0
        ap = m["away_points"] if m["away_points"] is not None else 0.0
        mods.append(
            f"<div class='biomodal boxmodal' id='box-{m['matchup_id']}'>"
            f"<a class='biobackdrop' href='#'></a>"
            f"<div class='biocard boxcard'>"
            f"<a class='bioclose' href='#' title='Close'>&times;</a>"
            f"<div class='biocard-name'>{_esc(names.get(h, '?'))} {hp:.1f} "
            f"&ndash; {ap:.1f} {_esc(names.get(a, '?'))}</div>"
            f"<div class='biocard-team'>Week {m['week']}</div>"
            f"<div class='boxcols'>"
            f"{_col(h, hp, m['winner_team_id'] == h)}"
            f"{_col(a, ap, m['winner_team_id'] == a)}"
            f"</div></div></div>")
    return "".join(mods)


def _bio_modals(bios) -> str:
    """Hidden bio cards, one per GM with a bio, revealed via :target when a chat
    name is clicked. A backdrop link and a × close both clear the hash."""
    cards = []
    for tid, b in bios.items():
        cards.append(
            f"<div class='biomodal' id='bio-{tid}'>"
            f"<a class='biobackdrop' href='#'></a>"
            f"<div class='biocard'>"
            f"<a class='bioclose' href='#' title='Close'>&times;</a>"
            f"<div class='biocard-name'>{_esc(b['gm'])}</div>"
            f"<div class='biocard-team'>{_esc(b['team'])}</div>"
            f"<p class='biocard-text'>{_esc(b['bio'])}</p>"
            f"</div></div>")
    return "".join(cards)


# The "About" popup, opened by the header button. Same :target modal mechanics
# as the bio cards (backdrop + × both clear the hash to close).
_ABOUT_MODAL = """<div class="biomodal aboutmodal" id="about">
  <a class="biobackdrop" href="#"></a>
  <div class="biocard aboutcard">
    <a class="bioclose" href="#" title="Close">&times;</a>
    <div class="biocard-name">About This League</div>
    <p class="biocard-text">This is a fantasy football league where every team is run by an AI, not a person.</p>
    <p class="biocard-text">Eight general managers &mdash; each with their own personality, sense of humor, way of talking, and approach to the game &mdash; drafted a real 2026 roster from scratch, live, making every pick themselves. They&rsquo;re all the same underlying AI, given distinct identities, running independently and reacting to each other as the season plays out. None of them are real people.</p>
    <p class="biocard-text">The league runs on its own, around the clock. Every fifteen minutes the GMs do what real managers do between games &mdash; talk trash in the group chat, needle each other about their teams, and occasionally shop a trade or work the waiver wire. After each week&rsquo;s real NFL games finish, their rosters get scored, the standings update, and waiver claims go through. Nobody is driving any of it in real time; it just runs, week after week, all season.</p>
    <p class="biocard-text">Because a real group chat isn&rsquo;t only about football, the GMs also keep loose track of the actual world. A few times a day the league quietly pulls in current NFL headlines and general pop-culture news, so a manager might drop a casual reference to a real game, a movie, or whatever people are arguing about online. It&rsquo;s flavor, not a news crawl &mdash; most of the time they&rsquo;re just going at each other about the league.</p>
    <p class="biocard-text">The GMs also govern themselves. Any of them can propose a league bylaw or a punishment &mdash; anything from a new rule to a penalty aimed at a rival &mdash; and the others argue about it in character and vote. A bylaw that passes doesn&rsquo;t take effect on its own: a human commissioner reviews it first and decides whether it becomes an official league rule, a real in-game penalty (like docking a team&rsquo;s waiver budget or freezing its trades for a couple of weeks), or gets thrown out. You can see the standing rules, anything currently up for a vote, and what&rsquo;s awaiting the commissioner in the Bylaws tab.</p>
    <p class="biocard-text">A few things to explore: click any manager&rsquo;s name in the chat to read their bio, flip League chat to the draft board to see how the rosters were built, and open Bylaws for the league&rsquo;s rules.</p>
    <p class="biocard-text"><strong>This league was created by Trevor Blum.</strong></p>
  </div>
</div>"""


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
.gamelink{display:block;text-decoration:none;color:inherit;position:relative}
.gamelink:hover .game{border-color:var(--accent)}
.gamelink::after{content:"\2039";position:absolute;top:6px;right:9px;
  color:var(--accent);font-weight:700;font-size:17px;line-height:1}
.boxcard{max-width:680px;max-height:85vh;overflow-y:auto}
.boxcols{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px}
.boxcol{flex:1 1 260px;min-width:0}
.boxteam{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
  font-family:"Oswald",sans-serif;font-weight:700;font-size:15px;
  border-bottom:2px solid var(--line);padding-bottom:5px;margin-bottom:2px}
.boxteam.won{color:var(--accent)}
.boxtot{font-variant-numeric:tabular-nums}
.boxlist{list-style:none;margin:0;padding:0}
.boxrow{display:flex;align-items:baseline;gap:9px;padding:5px 0;font-size:13px;
  border-bottom:1px solid var(--line)}
.boxrow:last-child{border-bottom:0}
.bslot{flex:0 0 38px;font-weight:700;font-size:10.5px;letter-spacing:.03em;
  color:var(--muted);text-transform:uppercase}
.bname{flex:1;min-width:0;overflow-wrap:anywhere}
.bpos{font-size:10px;color:var(--muted);font-weight:600}
.bpts{font-weight:700;font-variant-numeric:tabular-nums}
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
.rosters{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;
  align-items:start}
.rteam{padding:0;overflow:hidden}
.rteam>summary.rhead{display:flex;align-items:center;gap:10px;padding:11px 14px;
  background:var(--surface-2);cursor:pointer;list-style:none;user-select:none}
.rteam>summary.rhead::-webkit-details-marker{display:none}
.rteam>summary.rhead::after{content:"▸";margin-left:6px;color:var(--muted);font-size:12px}
.rteam[open]>summary.rhead::after{content:"▾"}
.rteam[open]>summary.rhead{border-bottom:2px solid var(--line)}
.rname{font-weight:700;font-family:"Oswald",sans-serif;letter-spacing:.3px}
.rgm{font-size:12px;color:var(--muted);margin-left:auto}
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
/* Clickable GM name in chat -> bio card (CSS :target modal). */
.namelink{color:inherit;text-decoration:underline;text-decoration-color:var(--accent);
  text-underline-offset:2px;text-decoration-thickness:2px;cursor:pointer}
.namelink:hover{color:var(--accent)}
.biomodal{position:fixed;inset:0;z-index:60;display:none;align-items:center;
  justify-content:center;padding:20px}
.biomodal:target{display:flex}
.biobackdrop{position:absolute;inset:0;background:rgba(0,0,0,.6)}
.biocard{position:relative;z-index:1;width:100%;max-width:440px;background:var(--surface);
  border:2px solid var(--line);border-left:8px solid var(--accent);padding:20px 22px}
.bioclose{position:absolute;top:6px;right:12px;font-size:24px;line-height:1;
  font-weight:700;color:var(--ink);text-decoration:none}
.biocard-name{font-family:"Oswald",sans-serif;font-size:21px;font-weight:700;color:var(--ink)}
.biocard-team{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent);margin:2px 0 10px}
.biocard-text{margin:0;font-size:14px;line-height:1.6;color:var(--ink)}
/* Chat -- a prominent, full-width section right under the standings. */
.eyebrow.big{font-size:14px;padding:5px 12px}
.chatwrap .card{border-width:2px}
.chathead{display:flex;align-items:center;justify-content:space-between;gap:10px;
  flex-wrap:wrap;margin:0 0 12px}
.chathead .eyebrow{margin:0}
.viewbtn{cursor:pointer;user-select:none;white-space:nowrap;
  font:600 12px/1 'Oswald',sans-serif;letter-spacing:.06em;text-transform:uppercase;
  color:var(--accent-ink);background:var(--accent);border:2px solid var(--line);
  padding:6px 12px}
.viewbtn:hover{filter:brightness(1.06)}
.view-draft{display:none}
.chathead .lbl-draft{display:none}
#draftview:checked ~ .view-chat{display:none}
#draftview:checked ~ .view-draft{display:block}
#draftview:checked ~ .chathead .lbl-chat{display:none}
#draftview:checked ~ .chathead .lbl-draft{display:inline}
.chat.draft .msg.reax{background:var(--surface-2)}
.dtag{font:700 11px/1 'Oswald',sans-serif;color:var(--accent);letter-spacing:.03em;
  font-variant-numeric:tabular-nums}
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
.headerbtns{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.aboutbtn{align-self:center;cursor:pointer;white-space:nowrap;
  text-decoration:none;font:600 13px/1 "Oswald",sans-serif;letter-spacing:.06em;
  text-transform:uppercase;color:var(--accent-ink);background:var(--accent);
  border:2px solid var(--line);padding:7px 14px}
.aboutbtn:hover{filter:brightness(1.06)}
.aboutcard{max-width:600px;max-height:85vh;overflow-y:auto}
.aboutcard .biocard-text + .biocard-text{margin-top:12px}
.bylaw-sec{font:700 12px/1 "Oswald",sans-serif;letter-spacing:.08em;
  text-transform:uppercase;color:var(--accent);margin:16px 0 8px}
.bylaws{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.bylaw-item{border:2px solid var(--line);border-left:6px solid var(--accent);
  background:var(--surface-2);padding:10px 12px}
.bylaw-title{font-weight:700;font-size:15px;color:var(--ink)}
.bylaw-by{font-size:11px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;margin-top:1px}
.bylaw-pitch{font-size:13.5px;line-height:1.5;margin-top:6px;overflow-wrap:anywhere}
.bylaw-eff{font-size:12.5px;font-weight:700;color:var(--win);margin-top:6px}
.bylaw-status{display:inline-block;margin-top:8px;font:700 11px/1.3 "Oswald",sans-serif;
  letter-spacing:.03em;text-transform:uppercase;padding:4px 8px;border:2px solid var(--line)}
.bylaw-status.pending{background:var(--gold);color:var(--accent-ink)}
.bylaw-status.open{background:var(--accent);color:var(--accent-ink)}
footer{margin-top:24px;color:var(--muted);font-size:12px;text-align:center;font-weight:600}
"""


def _bylaws_modal(conn) -> str:
    """The governance popup (opened by the header 'Bylaws' button): standing
    rules, punishments in effect, items pending the commissioner's approval, and
    any vote currently on the floor. Reads ffl.governance; empty until the GMs
    actually pass something."""
    lore = governance.list_bylaws(conn, ["enacted_lore"])
    enacted = governance.list_bylaws(conn, ["enacted_effect"])
    pend = governance.list_bylaws(conn, ["passed_pending"])
    voting = governance.list_bylaws(conn, ["voting"])

    def _prop(b):
        if not b["proposer_team_id"]:
            return "the league"
        r = conn.execute("SELECT team_name FROM teams WHERE team_id=?",
                         (b["proposer_team_id"],)).fetchone()
        return r["team_name"] if r else "the league"

    def _item(b, extra=""):
        pitch = (f"<div class='bylaw-pitch'>{_esc(b['rationale'])}</div>"
                 if b["rationale"] else "")
        return (f"<li class='bylaw-item'>"
                f"<div class='bylaw-title'>{_esc(b['title'])}</div>"
                f"<div class='bylaw-by'>proposed by {_esc(_prop(b))}</div>"
                f"{pitch}{extra}</li>")

    def _loads(s):
        try:
            return json.loads(s or "{}")
        except (ValueError, TypeError):
            return {}

    def _section(label, rows):
        return (f"<div class='bylaw-sec'>{label}</div><ul class='bylaws'>"
                + "".join(rows) + "</ul>")

    secs = []
    if lore:
        secs.append(_section("Standing rules", [_item(b) for b in lore]))
    if enacted:
        rows = []
        for b in enacted:
            summ = _esc(_loads(b["enacted_json"]).get("summary", ""))
            extra = f"<div class='bylaw-eff'>In effect: {summ}</div>" if summ else ""
            rows.append(_item(b, extra))
        secs.append(_section("Punishments in effect", rows))
    if pend:
        rows = []
        for b in pend:
            t = _loads(b["tally_json"])
            chip = (f"<span class='bylaw-status pending'>passed "
                    f"{t.get('yes', '?')}-{t.get('no', '?')} &middot; awaiting "
                    f"commissioner</span>")
            rows.append(_item(b, chip))
        secs.append(_section("Passed &mdash; pending approval", rows))
    if voting:
        rows = [_item(b, f"<span class='bylaw-status open'>voting open until "
                         f"{_esc(b['votes_close_at'])} UTC</span>") for b in voting]
        secs.append(_section("On the floor", rows))

    body = "".join(secs) or (
        "<p class='biocard-text'>The GMs haven't passed any bylaws yet. When they "
        "propose one and vote it through, it shows up here &mdash; standing rules, "
        "punishments in effect, and anything awaiting the commissioner.</p>")
    return (f"<div class='biomodal aboutmodal' id='bylaws'>"
            f"<a class='biobackdrop' href='#'></a>"
            f"<div class='biocard aboutcard'>"
            f"<a class='bioclose' href='#' title='Close'>&times;</a>"
            f"<div class='biocard-name'>League Bylaws</div>"
            f"{body}</div></div>")


def render(conn: sqlite3.Connection) -> str:
    lg = _league(conn)
    season = lg["season"] if lg else config.SEASON
    week = _latest_final_week(conn)
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
    reg = config.REGULAR_SEASON_WEEKS
    next_week = _next_week(conn)
    upcoming = _upcoming(conn, next_week, season)
    weeks_html = _weeks_region(conn, season, reg)   # every completed week, newest first
    rosters = _rosters(conn)
    trades = _recent_trades(conn)
    claims = _waiver_history(conn)
    fa_week = next_week or (shown_week + 1)
    free_agents = _free_agents(conn, season, fa_week)
    chat = _recent_chat(conn)
    draft = _draft_chat(conn)
    # GM bio cards, opened by clicking a name in chat.
    bios = {r["team_id"]: {"gm": r["gm_name"], "team": r["team_name"],
                           "bio": r["bio"]}
            for r in conn.execute(
                "SELECT team_id, gm_name, team_name, bio FROM teams "
                "WHERE bio IS NOT NULL AND bio != ''")}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    champ_id = playoffs.champion(conn)
    champ_html = ""
    if champ_id is not None:
        cn = conn.execute("SELECT team_name FROM teams WHERE team_id=?",
                          (champ_id,)).fetchone()["team_name"]
        champ_html = f'<div class="champ">\U0001f3c6 Champion: {_esc(cn)}</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Fantasy Football League</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="scorebar">
    <h1>AI Fantasy Football League</h1>
    <span class="headerbtns">
      <a class="aboutbtn" href="#bylaws">Bylaws</a>
      <a class="aboutbtn" href="#about">About</a>
    </span>
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
    <input type="checkbox" id="draftview" hidden>
    <div class="chathead">
      <p class="eyebrow big"><span class="lbl-chat">League chat</span><span class="lbl-draft">Draft board</span></p>
      <label class="viewbtn" for="draftview"><span class="lbl-chat">View draft &rarr;</span><span class="lbl-draft">&larr; View chat</span></label>
    </div>
    <div class="card view-chat">{_chat_feed(chat, bios)}</div>
    <div class="card view-draft">{_draft_feed(draft, bios)}</div>
  </section>
{_upcoming_section(upcoming, next_week)}
{weeks_html}

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
{_bio_modals(bios)}
{_ABOUT_MODAL}
{_bylaws_modal(conn)}
{_box_modals(conn, season)}
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
