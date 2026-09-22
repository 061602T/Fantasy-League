# Coding-agent effects: automating new bylaw mechanics

Most passed bylaws fit one of the four bounded effects the league already has
(`faab_adjust`, `trade_freeze`, `waiver_backseat`, `loser_flag`) — for those you
run `review_bylaws --auto <id>` and you're done. Occasionally the GMs pass a
bylaw whose intent needs a mechanic none of those can express. This pipeline
turns that case into: **a pull request shows up, you review and merge, you run
one command.**

## The flow

```
GMs pass a bylaw
      │
      ▼
dispatch_effects (on the Pi)  ── triage each new passed bylaw ──►  fits an existing effect?
      │                                                                 │ yes
      │ needs a new effect                                              ▼
      ▼                                                        marked "fits_existing";
files a "[gov-effect]" GitHub issue (brief in the body)        you run  review_bylaws --auto <id>
      │
      ▼
GitHub Action (anthropics/claude-code-action)  ── implements one new bounded effect ──►  opens a PR
      │
      ▼
YOU review + merge the PR        ← the only manual gate (never auto-merged)
      │
      ▼
git pull on the Pi  ──►  review_bylaws --auto <id>   (applies the effect: the human is always the executor)
```

Two safety lines are deliberate and are **not** removed by this automation:

1. **The PR is never merged automatically.** The coding agent writes new
   game-logic that runs on a public site; a human reads it before it ships.
2. **Applying teeth to a specific team stays a human command** (`--auto <id>`).
   Merging the PR says "this kind of effect may exist"; running `--auto` says
   "apply it, to this team, now." The codebase's core rule — *a human is always
   the executor of anything that changes game state* — is preserved.

## One-time setup (two clicks that have to be you)

1. **Install the Claude GitHub App** on this repo:
   <https://github.com/apps/claude> → Install → pick this repository. Grant the
   Contents / Issues / Pull requests permissions it asks for. (You need admin on
   the repo.)
2. **Add the API-key secret.** Repo → Settings → Secrets and variables →
   Actions → New repository secret → name `ANTHROPIC_API_KEY`, value = your
   Anthropic API key.

The workflow (`.github/workflows/draft-effect.yml`) is already committed. It runs
only on issues whose title starts with `[gov-effect]`, so it never touches
anything else.

> The workflow must live on the repo's **default branch** — GitHub runs
> issue-triggered workflows only from the default branch. That's why this ships
> on `main`.

## On the Pi

The dispatch job needs a GitHub token with **Issues: read/write** on this repo
and the repo name. It reuses the dashboard publisher's setup by default:

| env var | purpose | default |
| --- | --- | --- |
| `FFL_GH_AGENT_TOKEN` | token that files the issue | falls back to `FFL_GH_DASHBOARD_TOKEN` |
| `FFL_GH_CODE_REPO` | `owner/repo` to file issues on | the git `origin` remote of the checkout |

If your existing `FFL_GH_DASHBOARD_TOKEN` already has Issues write on this repo,
you don't need to add anything. If it's scoped to contents-only (or points at a
separate Pages repo), set `FFL_GH_AGENT_TOKEN` (and `FFL_GH_CODE_REPO` if
needed) to a token that can open issues here.

Run the triage job on a schedule (it's cheap and idempotent — a run with nothing
new to triage makes no API calls and opens nothing). Add to the Pi's crontab:

```cron
# Triage passed bylaws hourly; open at most one coding-agent issue per run.
CRON_TZ=America/New_York
0 * * * * cd /home/pi/Fantasy-League && venv/bin/python -m scripts.dispatch_effects >> ~/ffl-data/dispatch.log 2>&1
```

Or run it by hand any time:

```bash
python -m scripts.dispatch_effects            # triage; open <=1 issue
python -m scripts.dispatch_effects --dry-run  # classify + print, open nothing
python -m scripts.dispatch_effects --limit 2  # allow up to 2 new issues
```

## Checking status

`review_bylaws` shows, per pending bylaw, whether a coding-agent issue has been
filed (with its URL) or whether it was triaged as fitting an existing effect. So
the one place you already look to approve bylaws also tells you what the agent is
doing.
