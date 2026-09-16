"""One-off: publish the current dashboard to GitHub Pages, bypassing the tick.

A smoke test for the GitHub Pages wiring. It calls ``ghpages.publish()``
directly on the existing dashboard HTML using the real credentials
(``FFL_GH_DASHBOARD_*`` from the environment or a local ``.env``), ignoring the
tick's ``advanced or trade_happened`` gate -- so you can confirm push-to-GitHub
works end to end before a real advancing tick triggers it naturally.

It uses whatever dashboard file already exists (it does NOT regenerate one); run
a tick first if there isn't one yet -- every tick writes the dashboard, even an
idle one.

Usage:
    python -m scripts.publish_dashboard [--dashboard PATH]

Exit codes: 0 = published or already up to date; 2 = disabled (no token/repo);
1 = error (e.g. dashboard missing, or the push failed).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import ghpages


def _default_dashboard() -> str:
    # Best-effort .env load so FFL_DASHBOARD_PATH (and the GH vars) are honored
    # from a local .env, matching how ffl.ghpages resolves its config.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    return os.path.expanduser(os.environ.get(
        "FFL_DASHBOARD_PATH", os.path.join("~", "ffl-data", "dashboard.html")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", default=None,
                    help="dashboard HTML to publish (default: FFL_DASHBOARD_PATH "
                         "or ~/ffl-data/dashboard.html)")
    args = ap.parse_args()
    dashboard = args.dashboard or _default_dashboard()

    if not os.path.exists(dashboard):
        print(f"No dashboard at {dashboard}. Run a tick first "
              "(python -m scripts.run_tick) or pass --dashboard PATH.",
              file=sys.stderr)
        return 1

    cfg = ghpages.config_from_env()
    if cfg is None:
        print("Publishing is DISABLED: set FFL_GH_DASHBOARD_TOKEN and "
              "FFL_GH_DASHBOARD_REPO (in the environment or .env).",
              file=sys.stderr)
        return 2

    # Show the destination (repo/branch/dir) -- origin_url is the clean,
    # token-free URL, so nothing secret is printed.
    print(f"Publishing {dashboard}")
    print(f"  -> {cfg['origin_url']} (branch {cfg['branch']})")
    print(f"  clone dir: {cfg['dir']}")

    result = ghpages.publish(dashboard, cfg=cfg)
    print(f"Result: {result}")
    if result["status"] == "published":
        print("OK: pushed a new index.html. Give Pages a minute, then check "
              "your site URL.")
    elif result["status"] == "unchanged":
        print("OK: remote already matches this dashboard (nothing to push).")
    return 0 if result["status"] in ("published", "unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
