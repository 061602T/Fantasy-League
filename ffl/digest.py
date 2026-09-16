"""Notification digest -- a short text/Markdown summary alongside the dashboard.

Kept deliberately provider-agnostic: the tick writes the digest to a file and,
if configured, delivers it. Wire it to any push/email channel with one env var:

  FFL_DIGEST_WEBHOOK   POST the digest text to this URL (ntfy, Slack, a webhook)
  FFL_DIGEST_CMD       run this shell command with the digest on stdin, e.g.
                       'python3 -m scripts.send_digest_email' (Gmail SMTP),
                       'mail -s "FFL" me@x', or 'ntfy publish mytopic'

No third-party dependency and no hardcoded service, so it works on a headless
Pi however the owner already gets notifications.
"""
from __future__ import annotations

import os
import subprocess
import sqlite3
import urllib.request

from . import config, playoffs

DEFAULT_PATH = os.path.join("~", "ffl-data", "digest.txt")


def render_digest(conn: sqlite3.Connection, weeks_scored=None,
                  extra_events=None) -> str:
    lg = conn.execute("SELECT * FROM league WHERE id=1").fetchone()
    season = lg["season"] if lg else config.SEASON
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}

    out = [f"AI Fantasy League — {season} update"]

    champ = playoffs.champion(conn)
    if champ is not None:
        out.append(f"\U0001f3c6 CHAMPION: {names.get(champ, '?')}")

    latest = conn.execute(
        "SELECT MAX(week) w FROM matchups WHERE status='final'").fetchone()["w"]
    if latest:
        reg = config.REGULAR_SEASON_WEEKS
        label = f"Week {latest}" + ("" if latest <= reg else " (playoffs)")
        out.append(f"\n{label} results:")
        for m in conn.execute(
                """SELECT home_team_id, away_team_id, home_points, away_points,
                          winner_team_id FROM matchups
                    WHERE week=? AND status='final' ORDER BY matchup_id""",
                (latest,)):
            hi, lo = max(m["home_points"], m["away_points"]), \
                     min(m["home_points"], m["away_points"])
            w = m["winner_team_id"]
            if w is None:
                out.append(f"  {names[m['home_team_id']]} tied "
                           f"{names[m['away_team_id']]} {hi:.1f}–{lo:.1f}")
            else:
                loser = (m["away_team_id"] if w == m["home_team_id"]
                         else m["home_team_id"])
                out.append(f"  {names[w]} def. {names[loser]} {hi:.1f}–{lo:.1f}")

    top = conn.execute(
        """SELECT team_name, wins, losses, ties FROM teams
            ORDER BY wins DESC, points_for DESC LIMIT 4""").fetchall()
    if any(t["wins"] or t["losses"] or t["ties"] for t in top):
        out.append("\nStandings (top 4):")
        for i, t in enumerate(top, 1):
            out.append(f"  {i}. {t['team_name']} "
                       f"({t['wins']}-{t['losses']}-{t['ties']})")

    if extra_events:
        notable = [e for e in extra_events if "waiver" in e or "trade" in e
                   or "CHAMPION" in e or "bracket" in e]
        if notable:
            out.append("\nAlso: " + "; ".join(notable))

    return "\n".join(out)


def deliver(text: str) -> dict:
    """Best-effort delivery via the configured webhook and/or command."""
    result = {"webhook": None, "command": None}
    url = os.environ.get("FFL_DIGEST_WEBHOOK")
    if url:
        try:
            req = urllib.request.Request(
                url, data=text.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                result["webhook"] = f"HTTP {resp.status}"
        except Exception as e:  # noqa: BLE001 -- delivery must never crash a tick
            result["webhook"] = f"error: {e}"
    cmd = os.environ.get("FFL_DIGEST_CMD")
    if cmd:
        try:
            p = subprocess.run(cmd, shell=True, input=text, text=True,
                               capture_output=True, timeout=30)
            result["command"] = f"exit {p.returncode}"
        except Exception as e:  # noqa: BLE001
            result["command"] = f"error: {e}"
    return result


def publish(conn: sqlite3.Connection, path: str = None, weeks_scored=None,
            extra_events=None, do_deliver: bool = True) -> dict:
    """Render the digest, write it to disk, and deliver it if configured."""
    text = render_digest(conn, weeks_scored, extra_events)
    path = path or os.path.expanduser(
        os.environ.get("FFL_DIGEST_PATH", DEFAULT_PATH))
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    delivered = deliver(text) if do_deliver else None
    return {"path": path, "text": text, "delivered": delivered}
