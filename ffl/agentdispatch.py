"""File a GitHub issue that asks the Claude Code GitHub Action to draft a new
bounded governance effect (Pi deployment).

When a passed bylaw needs a mechanic the whitelist can't express, the dispatch
job (scripts/dispatch_effects.py, via ffl.governance.dispatch_pending) files an
issue titled ``[gov-effect] ...`` whose body is the coding-agent brief. The
workflow in ``.github/workflows/draft-effect.yml`` picks it up, implements the
effect on a branch, and opens a pull request for the commissioner to review and
merge. Nothing in this module writes league code or merges anything -- it only
opens an issue.

Auth (same hygiene as ffl.ghpages -- read from the environment at runtime, never
hardcoded, never written to a file, scrubbed from any logged error):

* ``FFL_GH_AGENT_TOKEN``  -- a GitHub token with Issues: read/write on the CODE
  repo. Falls back to ``FFL_GH_DASHBOARD_TOKEN`` (the token the dashboard
  publisher already uses) so a single fine-grained PAT can cover both.
* ``FFL_GH_CODE_REPO``    -- ``owner/repo`` (or a URL) of the code repo to file
  issues on. Falls back to this checkout's git ``origin`` remote.
* ``FFL_GH_API_BASE``     -- REST API base (optional; default
  ``https://api.github.com``; set for GitHub Enterprise).

The token is sent only as an ``Authorization`` header over HTTPS; it never
touches disk and is replaced with ``***`` in any error text before it is
returned or logged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

_API_BASE = "https://api.github.com"
_USER_AGENT = "ffl-agent-dispatch"


def _load_env_once() -> None:
    """Best-effort load of a gitignored .env (dev convenience), like ffl.ghpages."""
    if os.environ.get("FFL_GH_AGENT_TOKEN") or os.environ.get(
            "FFL_GH_DASHBOARD_TOKEN"):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass


def _normalize_repo(repo: str) -> str:
    """'owner/repo' from 'owner/repo', an https URL, or an scp-style remote."""
    r = repo.strip()
    r = re.sub(r"^https?://[^/]+/", "", r)
    r = re.sub(r"^git@[^:]+:", "", r)
    r = re.sub(r"\.git$", "", r).strip("/")
    return r


def _origin_repo() -> str | None:
    """owner/repo of this checkout's git 'origin', or None if unavailable."""
    try:
        p = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True)
    except Exception:  # noqa: BLE001 -- git missing / not a repo
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return _normalize_repo(p.stdout.strip())


def config_from_env() -> dict | None:
    """Build the dispatch config from the environment, or None if disabled.

    Dispatch is disabled (a no-op) unless a token AND a resolvable code repo are
    available -- so the job runs harmlessly anywhere they aren't configured.
    """
    _load_env_once()
    token = (os.environ.get("FFL_GH_AGENT_TOKEN")
             or os.environ.get("FFL_GH_DASHBOARD_TOKEN"))
    repo = os.environ.get("FFL_GH_CODE_REPO") or _origin_repo()
    if not token or not repo:
        return None
    return {
        "token": token,
        "repo": _normalize_repo(repo),
        "api_base": os.environ.get("FFL_GH_API_BASE", _API_BASE).rstrip("/"),
    }


def _scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def _post_issue(cfg: dict, title: str, body: str, labels) -> dict:
    """POST a new issue to the GitHub REST API. Raises on a network/API error."""
    url = f"{cfg['api_base']}/repos/{cfg['repo']}/issues"
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = list(labels)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        })
    with urllib.request.urlopen(req, timeout=30) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    return {"number": obj.get("number"), "url": obj.get("html_url")}


def open_effect_issue(*, title: str, body: str, labels=None, cfg: dict | None = None,
                      poster=None) -> dict:
    """Open a ``[gov-effect]`` issue for the coding agent. Never raises.

    Returns a status dict:
      disabled -- no token/repo configured (nothing filed)
      opened   -- issue created ('number' and 'url' set)
      error    -- the API call failed ('error' has a token-scrubbed message)

    `poster(cfg, title, body, labels)` is injectable for tests; it defaults to a
    real REST POST.
    """
    cfg = cfg if cfg is not None else config_from_env()
    if cfg is None:
        return {"status": "disabled",
                "error": "set FFL_GH_AGENT_TOKEN (or FFL_GH_DASHBOARD_TOKEN) and "
                         "FFL_GH_CODE_REPO to enable coding-agent dispatch"}
    poster = poster or _post_issue
    try:
        res = poster(cfg, title, body, labels)
        return {"status": "opened", "number": res.get("number"),
                "url": res.get("url")}
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        msg = _scrub(f"HTTP {e.code} filing issue: {detail[:300]}", cfg.get("token"))
        return {"status": "error", "error": msg}
    except Exception as e:  # noqa: BLE001 -- dispatch must never crash the job
        return {"status": "error", "error": _scrub(str(e), cfg.get("token"))}
