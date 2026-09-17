"""Refresh the cached real-world context the GMs can reference in chat.

A small, infrequent job -- run it on its own cron schedule (~every 4 hours),
separate from run_tick and the ambient chat loop, so a slow web search never
blocks scoring or chatter. It web-searches current NFL news and general
pop-culture/news via the Anthropic server-side web_search tool, distills the
results to a handful of short bullets, and writes them to the context cache
(FFL_CONTEXT_PATH, default ~/ffl-data/world_context.json).

Failure is non-fatal by design: if the search fails, web search isn't enabled
on the account, or nothing usable comes back, the existing cache is left
untouched and this exits 0 (a cron loop shouldn't hard-fail). Chat generation
reads the cache independently and simply runs league-only when it's absent or
stale, so nothing downstream errors out either way.

Usage:      python -m scripts.refresh_context [--path FILE] [--print]
Cron (4h):  0 */4 * * * cd /home/USER/Fantasy-League && \
    venv/bin/python -m scripts.refresh_context >> ~/ffl-data/context.log 2>&1
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, worldcontext


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=None,
                    help="context cache file (default: FFL_CONTEXT_PATH or "
                         "~/ffl-data/world_context.json)")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="print the bullets that were cached")
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    path = args.path or config.CONTEXT_PATH

    try:
        bullets = worldcontext.refresh(path)
    except Exception as e:  # noqa: BLE001 -- belt-and-suspenders; refresh already guards
        print(f"[{stamp}] context refresh error (kept old cache): {e}",
              file=sys.stderr)
        return 0

    if not bullets:
        print(f"[{stamp}] no fresh context (search failed or empty); "
              "kept any existing cache")
        return 0

    print(f"[{stamp}] cached {len(bullets)} context bullet(s) -> "
          f"{os.path.expanduser(path)}")
    if args.show:
        for b in bullets:
            print(f"    - {b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
