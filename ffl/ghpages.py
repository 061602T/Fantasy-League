"""Publish the dashboard HTML to a GitHub Pages repo (Pi deployment).

After an advancing tick, the freshly generated dashboard is copied into a
*separate* local clone of the Pages repo as ``index.html``, committed, and
pushed -- so the league has a public, always-current dashboard. This module:

* clones the Pages repo on first use (into ``FFL_GH_DASHBOARD_DIR``, default
  ``~/ffl-data/dashboard-repo`` -- deliberately outside the code checkout);
* only commits/pushes when ``index.html`` actually changed (idle ticks are
  skipped -- no empty commits);
* never raises: any git/network/auth failure is logged and swallowed so it can
  never crash the tick.

Auth. The token and repo are read from the environment at runtime -- never
hardcoded, never written to a tracked file:

* ``FFL_GH_DASHBOARD_TOKEN`` -- a GitHub token with push access.
* ``FFL_GH_DASHBOARD_REPO``  -- ``owner/repo`` or a full GitHub URL.
* ``FFL_GH_DASHBOARD_DIR``   -- where to keep the clone (optional).
* ``FFL_GH_DASHBOARD_BRANCH``-- Pages branch (optional, default ``main``).

The token is embedded in the push URL passed directly to ``git push`` at call
time; the clone's stored ``origin`` is reset to the clean (token-free) URL, so
the token is never persisted to ``.git/config`` on disk. It is also scrubbed
from any error text before logging.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_DIR = os.path.join("~", "ffl-data", "dashboard-repo")
_COMMIT_NAME = "FFL Tick Bot"
_COMMIT_EMAIL = "ffl-tick-bot@users.noreply.github.com"


def _load_env_once() -> None:
    """Best-effort load of a gitignored .env (dev convenience), like ffl.llm."""
    if os.environ.get("FFL_GH_DASHBOARD_TOKEN"):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass


def _normalize_repo(repo: str) -> str:
    """'owner/repo' from 'owner/repo', a URL, or an scp-style remote."""
    r = repo.strip()
    r = re.sub(r"^https?://[^/]+/", "", r)          # strip https://github.com/
    r = re.sub(r"^git@[^:]+:", "", r)               # strip git@github.com:
    r = re.sub(r"\.git$", "", r).strip("/")
    return r


def config_from_env() -> dict | None:
    """Build the publish config from the environment, or None if disabled.

    Publishing is disabled (a no-op) unless both the token and repo are set --
    so the tick runs unchanged anywhere they aren't configured.
    """
    _load_env_once()
    token = os.environ.get("FFL_GH_DASHBOARD_TOKEN")
    repo = os.environ.get("FFL_GH_DASHBOARD_REPO")
    if not token or not repo:
        return None
    owner_repo = _normalize_repo(repo)
    clean = f"https://github.com/{owner_repo}.git"
    token_url = f"https://x-access-token:{token}@github.com/{owner_repo}.git"
    return {
        "dir": os.path.expanduser(
            os.environ.get("FFL_GH_DASHBOARD_DIR", DEFAULT_DIR)),
        "branch": os.environ.get("FFL_GH_DASHBOARD_BRANCH", "main"),
        "clone_url": token_url,     # tokenized so a private repo can clone
        "push_url": token_url,      # passed to `git push` explicitly, never stored
        "origin_url": clean,        # what actually persists in .git/config
        "token": token,             # for scrubbing error text only
    }


def _git(args: list[str], cwd: str) -> str:
    """Run a git command, returning stdout; raise RuntimeError(stderr) on failure."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "git failed").strip())
    return p.stdout


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _ensure_clone(cfg: dict, git) -> None:
    """Clone the Pages repo if we don't have it yet, and pin a commit identity.

    After cloning with the tokenized URL, `origin` is reset to the clean URL so
    the token never lands in .git/config. A local user.name/email is set so
    commits work on a fresh Pi with no global git identity.
    """
    dest = cfg["dir"]
    if os.path.isdir(os.path.join(dest, ".git")):
        return
    parent = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(parent, exist_ok=True)
    git(["clone", cfg["clone_url"], dest], parent)
    git(["remote", "set-url", "origin", cfg["origin_url"]], dest)
    git(["config", "user.name", _COMMIT_NAME], dest)
    git(["config", "user.email", _COMMIT_EMAIL], dest)


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def publish(html_path: str, *, cfg: dict | None = None, git=None,
            quiet: bool = False) -> dict:
    """Copy `html_path` into the Pages clone as index.html, commit, and push.

    Returns a status dict; never raises. Status is one of:
      disabled  -- no token/repo configured (nothing to do)
      no_source -- the dashboard file couldn't be read
      unchanged -- index.html already matches (no commit made)
      published -- committed and pushed a new index.html
      error     -- something failed (details in 'error'); the tick continues
    """
    cfg = cfg if cfg is not None else config_from_env()
    if cfg is None:
        return {"status": "disabled"}
    git = git or _git
    token = cfg.get("token")
    try:
        new = _read(html_path)
        if new is None:
            return {"status": "no_source"}
        _ensure_clone(cfg, git)
        dest = os.path.join(cfg["dir"], "index.html")
        if _read(dest) == new:
            return {"status": "unchanged"}
        with open(dest, "w", encoding="utf-8") as f:
            f.write(new)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        git(["add", "index.html"], cfg["dir"])
        git(["commit", "-m", f"Update league dashboard ({stamp})"], cfg["dir"])
        git(["push", cfg["push_url"], f"HEAD:{cfg['branch']}"], cfg["dir"])
        return {"status": "published"}
    except Exception as e:  # noqa: BLE001 -- publishing must never crash a tick
        msg = _scrub(str(e), token)
        if not quiet:
            print(f"WARNING: dashboard publish failed: {msg}", file=sys.stderr)
        return {"status": "error", "error": msg}
