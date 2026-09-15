"""Run the live 15-round snake draft (real Sonnet picks).

Requires the 8 team personas to already exist (run scripts.gen_personas first).
Makes ~120 real Claude API calls -- ANTHROPIC_API_KEY must be set.

Usage:
    python -m scripts.run_draft [--db PATH] [--force]

--force clears any existing draft and re-drafts (this costs API calls).
"""
import argparse
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, draft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = db.init_db(args.db)
    n_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if n_teams < config.NUM_TEAMS:
        print(f"Only {n_teams} teams exist -- run `python -m scripts.gen_personas` "
              "first to create the 8 personas.")
        return 1

    def show(entry):
        quip = f'  “{entry["comment"]}”' if entry["comment"] else ""
        head = (f"  R{entry['round']:>2}.{entry['pick_in_round']} "
                f"(#{entry['overall']:>3})  {entry['name']:<24} "
                f"{entry['position']:<3}")
        print(head + quip)

    print(f"Drafting {config.DRAFT_ROUNDS} rounds with {config.NUM_TEAMS} GMs "
          f"via {draft.llm.MODEL_DECISION} ...\n")
    result = draft.run_draft(conn, force=args.force, progress=show)

    print("\n=== Final rosters ===")
    for r in conn.execute("SELECT team_id, team_name, gm_name, draft_slot "
                          "FROM teams ORDER BY draft_slot"):
        players = conn.execute(
            """SELECT p.position, p.name
                 FROM rosters ro JOIN players p ON p.player_id = ro.player_id
                WHERE ro.team_id = ? AND ro.acquired_via='draft'
                ORDER BY CASE p.position
                  WHEN 'QB' THEN 1 WHEN 'RB' THEN 2 WHEN 'WR' THEN 3
                  WHEN 'TE' THEN 4 WHEN 'K' THEN 5 WHEN 'DST' THEN 6 END, p.name""",
            (r["team_id"],)).fetchall()
        roster = ", ".join(f"{p['name']}({p['position']})" for p in players)
        print(f"\n[{r['draft_slot']}] {r['team_name']} -- GM {r['gm_name']} "
              f"({len(players)} players)\n    {roster}")

    print(f"\nDrafted {len(result['picks'])} players across "
          f"{config.NUM_TEAMS} teams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
