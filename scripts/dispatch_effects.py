"""File coding-agent issues for passed bylaws that need a brand-new effect.

A passed bylaw waits in 'passed_pending' for you. Most fit an existing bounded
effect -- for those you just run ``review_bylaws --auto <id>``. A few ask for a
mechanic the whitelist can't express; this job spots those and files a
``[gov-effect]`` GitHub issue whose body is a ready-made brief. The Claude Code
GitHub Action turns that issue into a pull request that adds the new effect. You
review and merge the PR (nothing merges itself), pull on the Pi, then enact with
``review_bylaws --auto <id>``.

Run it periodically (cron) or by hand:
    python -m scripts.dispatch_effects              # triage; open <=1 issue
    python -m scripts.dispatch_effects --limit 2    # allow up to 2 new issues
    python -m scripts.dispatch_effects --dry-run    # classify + print, open nothing
    python -m scripts.dispatch_effects --once 7     # only consider bylaw #7

It is cheap and idempotent: each bylaw is triaged once (the result is recorded on
the bylaw), so a run with nothing new to do makes no API calls and opens nothing.

Config (see ffl/agentdispatch.py): FFL_GH_AGENT_TOKEN (or FFL_GH_DASHBOARD_TOKEN)
with Issues: read/write, and FFL_GH_CODE_REPO (or the git 'origin' remote).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import agentdispatch, config, db, governance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--limit", type=int, default=1,
                    help="max new issues to open this run (default 1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and print; open nothing, change nothing")
    ap.add_argument("--once", type=int, metavar="ID",
                    help="only consider this one bylaw id")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league db at {args.db}.", file=sys.stderr)
        return 1

    if not args.dry_run and agentdispatch.config_from_env() is None:
        print("Coding-agent dispatch is not configured: set FFL_GH_AGENT_TOKEN "
              "(or FFL_GH_DASHBOARD_TOKEN) and FFL_GH_CODE_REPO. Use --dry-run to "
              "triage without filing issues.", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    results = governance.dispatch_pending(
        conn, limit=args.limit, dry_run=args.dry_run, only_id=args.once)

    if not results:
        print("Nothing to triage (no untriaged passed bylaws).")
        return 0
    for r in results:
        print(f"  #{r['bylaw_id']:>3}  {r['action']:<15} {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
