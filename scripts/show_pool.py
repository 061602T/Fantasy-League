"""Print the current draft pool and per-position leaders.

Usage: python -m scripts.show_pool   (from repo root)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import projections as proj, config


def main():
    pool = proj.build_player_pool()
    print(f"Draft pool: {len(pool)} players "
          f"(as of {config.SEASON} week {config.AS_OF_WEEK})")
    print("Positional counts:", pool.position.value_counts().to_dict())
    print()
    for pos in ["QB", "RB", "WR", "TE", "K", "DST"]:
        sub = pool[pool.position == pos].sort_values("proj_ppg", ascending=False)
        print(f"--- {pos} (top 10 of {len(sub)}) ---")
        for i, (_, r) in enumerate(sub.head(10).iterrows(), 1):
            print(f"  {i:2}. {r['name']:<26} {r['team'] or '':<4} "
                  f"{r['proj_ppg']:>6.2f} ppg  ({int(r['n_games'])}g)")
        print()


if __name__ == "__main__":
    main()
