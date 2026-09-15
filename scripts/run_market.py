"""Run live trades and/or waivers (real Sonnet + Haiku calls).

Requires an existing drafted league. Makes real Claude API calls.

Usage:
    python -m scripts.run_market --db league.db --trade 1 2      # slot1 -> slot2
    python -m scripts.run_market --db league.db --waivers [--week N] [--teams 1 2 3]

A trade or waiver permanently changes rosters, so a backup is taken afterward.
"""
import argparse
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup, config, db, market, rosters, projections


def _proj_map():
    return {r.entity_id: r.proj_ppg
            for r in projections.build_projections().itertuples(index=False)}


def _slot_to_team(conn):
    return {r["draft_slot"]: r["team_id"]
            for r in conn.execute("SELECT team_id, draft_slot FROM teams")}


def show_roster(conn, tid):
    t = conn.execute("SELECT team_name, gm_name FROM teams WHERE team_id=?", (tid,)).fetchone()
    players = ", ".join(f"{r['name']}({r['position']})"
                        for r in rosters.active_roster(conn, tid))
    print(f"  {t['team_name']} (GM {t['gm_name']}): {players}")


def do_trade(conn, proj_map, from_slot, to_slot):
    s2t = _slot_to_team(conn)
    a, b = s2t[from_slot], s2t[to_slot]
    print("Before:")
    show_roster(conn, a); show_roster(conn, b)
    print(f"\nNegotiating: slot {from_slot} -> slot {to_slot} ...\n")
    res = market.negotiate(conn, a, b, proj_map)
    print(f"Result: {res['status']} after {res['rounds']} round(s).")
    print("\nTranscript:")
    for r in conn.execute(
            "SELECT message FROM chat_log WHERE event_type='trade_talk' "
            "ORDER BY chat_id"):
        print(f"  - {r['message']}")
    if res["status"] == "accepted":
        print("\nAfter:")
        show_roster(conn, a); show_roster(conn, b)


def do_waivers(conn, proj_map, week, team_slots):
    s2t = _slot_to_team(conn)
    team_ids = [s2t[s] for s in team_slots] if team_slots else None
    fa = market.free_agents(conn, proj_map)
    print("Top free agents:")
    for f in fa[:8]:
        print(f"  {f['name']} ({f['position']}, proj {f['proj']:.1f})")
    print(f"\nRunning waivers for week {week} ...\n")
    results = market.run_waivers(conn, week=week, team_ids=team_ids,
                                 proj_map=proj_map)
    if not results:
        print("No team chose to make a claim.")
    for r in conn.execute("SELECT message FROM chat_log WHERE event_type='waiver' "
                          "ORDER BY chat_id"):
        print(f"  - {r['message']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--trade", nargs=2, type=int, metavar=("FROM_SLOT", "TO_SLOT"))
    ap.add_argument("--waivers", action="store_true")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--teams", nargs="*", type=int, default=None,
                    help="draft slots to include in waivers (default: all)")
    args = ap.parse_args()

    conn = db.init_db(args.db)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("No league found -- run gen_personas + run_draft first.")
        return 1
    if not args.trade and not args.waivers:
        print("Nothing to do: pass --trade FROM TO and/or --waivers.")
        return 1

    proj_map = _proj_map()
    if args.trade:
        do_trade(conn, proj_map, args.trade[0], args.trade[1])
    if args.waivers:
        week = args.week or rosters.current_week(conn)
        do_waivers(conn, proj_map, week, args.teams)

    try:
        dest = backup.backup_db(db_path=args.db)
        if dest:
            print(f"\nPost-market backup written: {dest}")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: post-market backup failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
