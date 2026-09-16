"""Offline tests for GitHub Pages dashboard publishing -- no network, no git.

A fake git runner records commands and simulates the clone directory, so the
change-detection, clone-once, failure-handling, and token-scrubbing logic can
be checked deterministically. Run:  python -m scripts.test_ghpages
"""
import sys, os, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import ghpages


class FakeGit:
    """Records git calls; 'clone' materializes the target dir with a .git marker.
    `fail_on` makes the named subcommand raise, to simulate push/auth failures."""
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        if args[0] == "clone":
            os.makedirs(os.path.join(args[2], ".git"), exist_ok=True)
        if self.fail_on and args[0] == self.fail_on:
            raise RuntimeError(f"simulated {self.fail_on} failure")
        return ""

    def count(self, sub):
        return sum(1 for a in self.calls if a and a[0] == sub)


def _cfg(dirp, token=None):
    url = "file:///tmp/fake-remote.git"
    return {"dir": dirp, "branch": "main", "clone_url": url, "push_url": url,
            "origin_url": url, "token": token}


def _src(tmp, content):
    p = os.path.join(tmp, "dash.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def test_first_publish_clones_and_pushes():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(os.path.join(tmp, "clone"))
    git = FakeGit()
    r = ghpages.publish(_src(tmp, "<html>v1</html>"), cfg=cfg, git=git)
    assert r["status"] == "published", r
    assert git.count("clone") == 1 and git.count("commit") == 1 and git.count("push") == 1
    # origin reset to the clean URL; index.html written with the source content.
    assert ["remote", "set-url", "origin", cfg["origin_url"]] in git.calls
    assert ghpages._read(os.path.join(cfg["dir"], "index.html")) == "<html>v1</html>"
    print("ok: first publish (clones once, resets origin, commits + pushes)")


def test_unchanged_skips_commit():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(os.path.join(tmp, "clone"))
    git = FakeGit()
    src = _src(tmp, "<html>same</html>")
    ghpages.publish(src, cfg=cfg, git=git)             # first: publishes
    r = ghpages.publish(src, cfg=cfg, git=git)         # second: identical
    assert r["status"] == "unchanged", r
    assert git.count("clone") == 1, "must not re-clone an existing checkout"
    assert git.count("commit") == 1 and git.count("push") == 1, "no empty commit"
    print("ok: unchanged dashboard -> no second commit/push (no empty commits)")


def test_changed_publishes_again():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(os.path.join(tmp, "clone"))
    git = FakeGit()
    src = _src(tmp, "<html>v1</html>")
    ghpages.publish(src, cfg=cfg, git=git)
    with open(src, "w", encoding="utf-8") as f:
        f.write("<html>v2</html>")
    r = ghpages.publish(src, cfg=cfg, git=git)
    assert r["status"] == "published" and git.count("commit") == 2
    print("ok: changed dashboard -> a new commit + push")


def test_push_failure_is_handled_and_scrubbed():
    tmp = tempfile.mkdtemp()
    cfg = _cfg(os.path.join(tmp, "clone"), token="SECRETTOKEN")
    # A push that raises with the token in the message (as git can) -> scrubbed.
    class FailingPush(FakeGit):
        def __call__(self, args, cwd):
            super().__call__(args, cwd)
            if args[0] == "push":
                raise RuntimeError("fatal: https://x-access-token:SECRETTOKEN@github.com/o/r.git denied")
    r = ghpages.publish(_src(tmp, "<html>x</html>"), cfg=cfg, git=FailingPush(), quiet=True)
    assert r["status"] == "error", r
    assert "SECRETTOKEN" not in r["error"] and "***" in r["error"], r
    print("ok: push failure returns error (tick continues), token scrubbed")


def test_disabled_without_env(monkeyless=True):
    for var in ("FFL_GH_DASHBOARD_TOKEN", "FFL_GH_DASHBOARD_REPO"):
        os.environ.pop(var, None)
    # No .env token either in this container, so config is None -> disabled no-op.
    assert ghpages.config_from_env() is None
    assert ghpages.publish("/nonexistent.html")["status"] == "disabled"
    print("ok: no token/repo configured -> publish is a disabled no-op")


def test_normalize_repo():
    for given in ("owner/repo", "https://github.com/owner/repo",
                  "https://github.com/owner/repo.git", "git@github.com:owner/repo.git"):
        assert ghpages._normalize_repo(given) == "owner/repo", given
    print("ok: _normalize_repo (owner/repo from every remote form)")


def main():
    test_first_publish_clones_and_pushes()
    test_unchanged_skips_commit()
    test_changed_publishes_again()
    test_push_failure_is_handled_and_scrubbed()
    test_disabled_without_env()
    test_normalize_repo()
    print("\nALL OFFLINE GITHUB-PAGES TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
