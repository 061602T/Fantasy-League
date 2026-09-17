"""Print a full governance cycle end-to-end with MOCKED data -- no API, no live
DB. Uses the real governance/effects code against a throwaway in-memory league,
so what you see is exactly how the built system behaves. Run:
    python -m scripts.gov_demo
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, effects, governance

_TEAMS = [
    ("Petty Cash Only", "Nia Robertson", "trash-talker"),
    ("Reasonable Doubt", "Tom Alvarez", "moderate"),
    ("Litigation Nation", "Priya Chandrasekhar", "moderate"),
    ("Chaos Theory FC", "Gail Fitzpatrick", "trash-talker"),
    ("Thee Vibes Only", "Marcus Webb", "moderate"),
    ("Bees? In THIS Economy", "Dale Prentiss", "trash-talker"),
    ("Spreadsheet Supremacy", "Renata Okafor", "quiet"),
    ("Slow News Day", "Ben Osei", "quiet"),
]

# Scripted, in-character votes keyed by team_id (proposer auto-votes yes).
_VOTES = {
    2: ("no", "This is targeted harassment and I've screenshotted it."),
    3: ("yes", "Procedurally sound. The motion carries in spirit."),
    4: ("yes", "chaos tax? im in, sorry tom"),
    5: ("no", "leave the man's FAAB alone, vibes are off"),
    6: ("yes", "back home we'd have taken more than 25 but sure"),
    7: ("abstain", "No data supports either side. Abstaining."),
    # team 8 (Ben) never votes -> counts as no-show
}


def _seed():
    conn = db.init_db(":memory:")
    conn.execute("INSERT INTO league(id,season,as_of_week,current_week,num_teams,"
                 "status) VALUES(1,2026,1,1,8,'regular')")
    for i, (team, gm, chat) in enumerate(_TEAMS, start=1):
        faab = 80 if team == "Reasonable Doubt" else 100
        conn.execute("INSERT INTO teams(team_id,team_name,gm_name,personality,bio,"
                     "chattiness,draft_slot,faab_remaining) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (i, team, gm, "runs it hard", "a bio", chat, i, faab))
    conn.commit()
    return conn


def _chat_tail(conn, n=20):
    return conn.execute(
        "SELECT message FROM chat_log WHERE event_type='bylaw' "
        "ORDER BY chat_id DESC LIMIT ?", (n,)).fetchall()[::-1]


def main():
    conn = _seed()

    # 1) Propose (mock the model's proposal text).
    governance.llm.chat_json = lambda *a, **k: {
        "title": "Tax the Hoarder",
        "pitch": ("Nine RBs and a fear of every waiver? Tax the hoarder. Minus 25 "
                  "FAAB and maybe Tom learns to live dangerously.")}
    b = governance.propose(conn, 1)   # Nia (Petty Cash Only)
    bid = b["bylaw_id"]

    # 2) Everyone else votes (mock per-GM votes).
    governance._cast_vote = lambda conn, bylaw, team: (
        {"vote": _VOTES[team["team_id"]][0], "message": _VOTES[team["team_id"]][1]}
        if team["team_id"] in _VOTES else None)
    governance.cast_missing_votes(conn, bid)

    print("=== CHAT FEED (event_type='bylaw') ===")
    for r in _chat_tail(conn):
        print(" ", r["message"])

    # 3) Close the window and tally.
    later = datetime.now(timezone.utc) + timedelta(hours=99)
    [outcome] = governance.close_if_due(conn, now=later)
    print("\n=== TALLY AT CLOSE ===")
    print(f"  yes={outcome['yes']} no={outcome['no']} abstain={outcome['abstain']} "
          f"-> {outcome['status']} ({outcome['reason']})")

    # 4) What the commissioner sees as pending.
    print("\n=== PENDING YOUR APPROVAL ===")
    for p in governance.pending(conn):
        print(f'  #{p["bylaw_id"]}  "{p["title"]}"  (nothing applied yet)')

    # 5) Commissioner enacts it as a bounded mechanical effect.
    before = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=2"
                          ).fetchone()[0]
    tid = effects.team_id_by_name(conn, "Reasonable Doubt")
    ok, summary = governance.enact_effect(conn, bid, "faab_adjust", tid, {"delta": -25})
    after = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=2"
                         ).fetchone()[0]
    print("\n=== COMMISSIONER ENACTS (review_bylaws --effect ...) ===")
    print(f"  {summary}")
    print(f"  Reasonable Doubt FAAB: {before} -> {after}   (bylaw status now "
          f"'enacted_effect')")
    print("\n(Everything above ran the real governance/effects code on mock data. "
          "Nothing touched a live DB or the tick loop.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
