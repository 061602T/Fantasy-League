"""Preview how the GMs reference real-world context in chat -- READ-ONLY.

Generates a few sample ambient chat lines from the real personas with the
world-context feature turned on, and PRINTS them. It does not write to chat_log,
does not publish the dashboard, and does not touch the production context cache
(it uses a throwaway temp cache). Safe to run against the live DB.

By default it seeds a few illustrative mock bullets so you can see the wiring
without depending on web search; pass --live-search to run the real
scripts.refresh_context search first and sample against genuinely current news.

To make the effect visible, context is FORCED ON for every sample line here. In
production only ~config.CONTEXT_INJECT_PROB of messages even see the context,
and the prompt tells the GM to reference it only rarely -- so real chat has far
fewer real-world asides than this preview.

Usage:
    python -m scripts.sample_context_chat [--db PATH] [--n 8] [--live-search]
"""
import argparse
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, worldcontext
from ffl import chat as chatmod

_MOCK_BULLETS = [
    "Star RB tweaked an ankle Sunday and is now week-to-week",
    "A backup QB nobody rostered threw five touchdowns in a blowout",
    "The primetime game came down to a 58-yard field goal at the buzzer",
    "A new sci-fi blockbuster just smashed opening-weekend box office records",
    "Everyone online is arguing about a surprise album that dropped at midnight",
    "A dating-show finale is the only thing group chats can talk about",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--n", type=int, default=8, help="how many sample lines")
    ap.add_argument("--live-search", action="store_true",
                    help="run the real web-search refresh first instead of "
                         "using the built-in mock bullets")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league db at {args.db}.", file=sys.stderr)
        return 1

    # Point the context layer at a throwaway cache and force it on for every
    # sample, so we never disturb the production cache and always see the effect.
    prod_prob = config.CONTEXT_INJECT_PROB   # the real per-message chance
    tmp = os.path.join(tempfile.mkdtemp(), "sample_context.json")
    config.CONTEXT_PATH = tmp
    config.CONTEXT_INJECT_PROB = 1.0

    if args.live_search:
        print("Running the real web-search refresh (this makes one API call)...")
        bullets = worldcontext.refresh(tmp)
        if not bullets:
            print("Live search returned nothing (web search may not be enabled "
                  "on this account); falling back to mock bullets.")
            worldcontext.write(_MOCK_BULLETS, tmp)
            bullets = _MOCK_BULLETS
    else:
        worldcontext.write(_MOCK_BULLETS, tmp)
        bullets = _MOCK_BULLETS

    print("\nCached context the GMs can see for this preview:")
    for b in bullets:
        print(f"  - {b}")
    print(f"\nGenerating {args.n} sample line(s) (context forced ON; "
          "read-only, nothing saved)\n" + "-" * 60)

    conn = db.connect(args.db)
    teams = [dict(r) for r in conn.execute("SELECT * FROM teams")]
    if not teams:
        print("No teams in the league yet.", file=sys.stderr)
        return 1

    recent = chatmod.recent_chat(conn)
    for _ in range(args.n):
        team = random.choice(teams)
        mode = "reply" if random.random() < 0.4 and recent != "(quiet so far)" \
            else "fresh"
        # Direct compose call -- no gate, no DB write, no post.
        line = chatmod._ambient_line(conn, team, recent, mode)
        if line:
            print(f"[{mode:5}] {team['gm_name']} ({team['team_name']}): {line}")
    print("-" * 60)
    print("Note: every line above saw the context. In production only "
          f"~{prod_prob:.0%} of messages do, and most of those still won't "
          "reference it -- expect real-world asides to be occasional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
