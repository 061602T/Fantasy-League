"""Commissioner review of GM-proposed bylaws -- the manual approval gate.

Passed bylaws never auto-execute; they wait in 'passed_pending' for you. This
script lists what's waiting and lets you enact each one exactly one of three
ways, or reject it. Nothing touches game state until you run an enact command.

List (default -- shows open votes, pending approvals, and standing lore):
    python -m scripts.review_bylaws

Approve with teeth in ONE command -- the model picks the single best bounded
effect from the whitelist and applies it (add --dry-run to preview first):
    python -m scripts.review_bylaws --auto 7
    python -m scripts.review_bylaws --auto 7 --dry-run

Enact a passed bylaw as a standing, display-only league rule (option B):
    python -m scripts.review_bylaws --lore 7

Enact a passed bylaw as ONE bounded mechanical effect (option C):
    python -m scripts.review_bylaws --effect 7 --type faab_adjust \
        --team "Reasonable Doubt" --delta -25
    python -m scripts.review_bylaws --effect 7 --type trade_freeze \
        --team "Thee Vibes Only" --weeks 2
    python -m scripts.review_bylaws --effect 7 --type loser_flag \
        --team "Slow News Day" --label "must draft in a clown costume"

Reject a passed bylaw:
    python -m scripts.review_bylaws --reject 7 --reason "too far, even for us"

Effects and their params (all bounds-checked in ffl/effects.py):
    faab_adjust      --team, --delta      (|delta| <= GOV_FAAB_MAX_DELTA)
    trade_freeze     --team, --weeks      (1..GOV_FREEZE_MAX_WEEKS)
    waiver_backseat  --team, --weeks      (1..GOV_BACKSEAT_MAX_WEEKS)
    loser_flag       --team, --label      (label sanitized, <= GOV_LOSER_LABEL_MAX)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, effects, governance


def _team_name(conn, tid):
    if tid is None:
        return "League"
    r = conn.execute("SELECT team_name FROM teams WHERE team_id=?", (tid,)).fetchone()
    return r["team_name"] if r else f"team {tid}"


def _print_list(conn):
    voting = governance.list_bylaws(conn, ["voting"])
    pend = governance.pending(conn)
    lore = governance.active_lore(conn)

    print("=== Open for voting ===")
    if not voting:
        print("  (none)")
    for b in voting:
        print(f"  #{b['bylaw_id']}  \"{b['title']}\"  by "
              f"{_team_name(conn, b['proposer_team_id'])}  (closes {b['votes_close_at']})")

    print("\n=== Passed, PENDING your approval ===")
    if not pend:
        print("  (none)")
    for b in pend:
        t = json.loads(b["tally_json"] or "{}")
        print(f"  #{b['bylaw_id']}  \"{b['title']}\"  "
              f"(voted {t.get('yes','?')}-{t.get('no','?')}, "
              f"by {_team_name(conn, b['proposer_team_id'])})")
        print(f"       pitch: {b['rationale']}")
        print(f"       approve w/ teeth: review_bylaws --auto {b['bylaw_id']}"
              f"   (model picks the effect; add --dry-run to preview)")
        print(f"       enact as lore:    review_bylaws --lore {b['bylaw_id']}")
        print(f"       effect by hand:   review_bylaws --effect {b['bylaw_id']} "
              f"--type <faab_adjust|trade_freeze|waiver_backseat|loser_flag> "
              f"--team \"<name>\" ...")
        print(f"       reject:           review_bylaws --reject {b['bylaw_id']} "
              f"--reason \"...\"")

    print("\n=== Standing league rules (enacted as lore) ===")
    if not lore:
        print("  (none)")
    for b in lore:
        print(f"  #{b['bylaw_id']}  \"{b['title']}\"")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--lore", type=int, metavar="ID",
                    help="enact bylaw ID as a display-only standing rule")
    ap.add_argument("--effect", type=int, metavar="ID",
                    help="enact bylaw ID as one bounded mechanical effect")
    ap.add_argument("--type", choices=sorted(effects.EFFECTS),
                    help="effect type (with --effect)")
    ap.add_argument("--team", help="target team NAME (with --effect)")
    ap.add_argument("--delta", type=int, help="faab_adjust delta (with --effect)")
    ap.add_argument("--weeks", type=int, help="freeze/backseat weeks (with --effect)")
    ap.add_argument("--label", help="loser_flag label (with --effect)")
    ap.add_argument("--auto", type=int, metavar="ID",
                    help="approve bylaw ID and let the model pick + apply the "
                         "single best bounded effect (still validated)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --auto, show the effect it would apply without "
                         "changing anything")
    ap.add_argument("--reject", type=int, metavar="ID", help="reject bylaw ID")
    ap.add_argument("--reason", default="", help="reason (with --reject)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league db at {args.db}.", file=sys.stderr)
        return 1
    conn = db.connect(args.db)

    chosen = [x for x in (args.lore, args.effect, args.auto, args.reject)
              if x is not None]
    if len(chosen) > 1:
        print("Choose only one of --lore / --effect / --auto / --reject.",
              file=sys.stderr)
        return 2

    if args.auto is not None:
        ok, msg = governance.enact_auto(conn, args.auto, dry_run=args.dry_run)
        print(msg if ok else f"error: {msg}",
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if args.lore is not None:
        ok, msg = governance.enact_lore(conn, args.lore)
        print(msg if ok else f"error: {msg}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1

    if args.effect is not None:
        if not args.type or not args.team:
            print("--effect requires --type and --team.", file=sys.stderr)
            return 2
        tid = effects.team_id_by_name(conn, args.team)
        if tid is None:
            print(f"No team named {args.team!r}.", file=sys.stderr)
            return 2
        params = {}
        if args.delta is not None:
            params["delta"] = args.delta
        if args.weeks is not None:
            params["weeks"] = args.weeks
        if args.label is not None:
            params["label"] = args.label
        ok, msg = governance.enact_effect(conn, args.effect, args.type, tid, params)
        print(msg if ok else f"error: {msg}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1

    if args.reject is not None:
        ok, msg = governance.reject(conn, args.reject, args.reason)
        print(msg if ok else f"error: {msg}", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1

    _print_list(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
