# Scripts

## Installing `lookup` as a global command

`scripts/install-lookup.sh` makes the `lookup` CLI runnable from anywhere. Because `lookup.py` is a thin HTTP client (its only runtime dependency is `httpx`), the installer builds a small isolated virtualenv containing just `httpx` and drops a `lookup` launcher on your PATH that runs this repo's `scripts/lookup.py` through that venv — it does **not** pull in the FastAPI/Groq service dependency tree.

```bash
scripts/install-lookup.sh              # install or update the `lookup` command
lookup "play la paradoja by juana molina"
lookup --staging "jessica pratt on your own love again"
scripts/install-lookup.sh --uninstall  # remove the launcher and its venv
```

The launcher points back at this repo, so:
- edits to `scripts/lookup.py` take effect immediately, with no reinstall, and
- the repo must stay where it is — moving or deleting it, or pruning the git worktree you installed from, breaks `lookup` (re-run the installer from the new location to fix it).

Defaults are overridable via environment variables: `PYTHON` (interpreter used to build the venv, default `python3`, requires >= 3.10), `LOOKUP_VENV` (venv location, default `$XDG_DATA_HOME/wxyc/lookup-venv`), and `BIN_DIR` (launcher directory, default `~/.local/bin`). If `BIN_DIR` isn't on your PATH the installer prints the line to add to your shell profile.

The installer's control flow (arg dispatch, guard clauses) is covered by fast, network-free tests in `tests/unit/test_install_lookup_cli.py` that run in the default suite. The full build-a-real-venv path is covered by `slow`-marked end-to-end tests in `tests/integration/test_install_lookup.py` (run them with `pytest -m slow`).

## Manual Testing Tools
- **`scripts/lookup.py`** - One-off lookups against production (default) or local (`--local`).
- **`scripts/repl.py`** - Interactive REPL with command history, server switching (`:local`/`:prod`)
- **`scripts/create_posthog_dashboard.py`** - Creates PostHog dashboard for telemetry visualization (requires `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_ID`)

## CI Helpers
- **`scripts/nlp_nightly_gate.py`** - Decides whether tonight's Groq NLP validation run is needed: picks the DST-correct 03:00 ET cron entry and diffs the NLP surface against the last green run. Called by `.github/workflows/nlp-nightly.yml`; see [Nightly NLP Check](testing.md#nightly-nlp-check-conditional).
- **`scripts/wait_for_railway_deployment.sh`** - Polls the Railway deployment API for a terminal status, so the CI smoke test does not race the rollout.
