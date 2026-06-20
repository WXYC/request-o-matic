# Deployment

- Hosted on Railway
- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
- Use `railway` CLI for status/logs (requires TTY for some commands)

## Dependency management

`uv.lock` is the **single source of truth** for dependency versions. `requirements.txt` and `requirements-dev.txt` are **generated artifacts** exported from the lock — never hand-edit them (the header on each records the exact command that produced it). This keeps the three install paths on identical versions:

| Path | Installs from | Used by |
|---|---|---|
| `uv sync` | `uv.lock` | local development |
| `pip install -r requirements.txt` | runtime pins only | Railway (`Dockerfile`) |
| `pip install -r requirements-dev.txt` | runtime + dev-tool pins | CI lint, typecheck, test, external-api |

Before this was reconciled (issue #168) the manifests had drifted: `pyproject.toml`/`requirements.txt` were floor-only (`fastapi>=0.104.0`) while the lock was frozen at an older version, so CI and Railway silently floated onto every new release while local `uv` stayed pinned. That skew shipped a FastAPI 0.137 breaking change into CI invisibly (issue #164). The fix: pin everything to the lock, and gate it.

**Pinning policy.** Load-bearing frameworks carry an upper bound in `pyproject.toml` so an unscoped `uv lock --upgrade` can't silently cross a breaking release. FastAPI is pre-1.0, so its **minor** bumps can break — it is capped at `<0.138` (the 0.137.x line currently deployed). `wxyc-fastapi` is capped at `<2.0.0`. `starlette` is intentionally *not* capped here — it is a FastAPI transitive dependency and FastAPI's own pin governs it; the lock records the exact resolved version. Dependency bumps are therefore explicit, reviewable commits, not ambient drift.

**Regenerate after any dependency change** (editing `pyproject.toml`, or running `uv lock`/`uv add`):

```bash
uv lock                                                                          # if pyproject.toml changed
uv export --frozen --no-emit-project --no-hashes -o requirements.txt             # runtime
uv export --frozen --extra dev --no-emit-project --no-hashes -o requirements-dev.txt   # runtime + dev tools
```

Commit `pyproject.toml`, `uv.lock`, and both `requirements*.txt` together.

**Bumping a capped framework** (deliberate, in its own PR):

```bash
# 1. widen/raise the cap in pyproject.toml (e.g. fastapi<0.139)
uv lock --upgrade-package fastapi   # 2. advance the lock
# 3. regenerate requirements*.txt (commands above), run the suite, then commit
```

**The gate.** The `deps-sync` job in `ci.yml` enforces the invariant: it runs `uv lock --locked` (proves the lock matches `pyproject.toml`) and re-exports both `requirements*.txt`, failing if they differ from what's committed. `deploy-staging` and `deploy-production` depend on it, so drift blocks deploys. The job pins `uv` (currently `uv==0.11.21`); keep that version aligned with the `uv` used to run the export commands above, since the export format is uv-version-sensitive and a mismatch would false-positive the drift check.

## CI pin maintenance

Two pins in `.github/workflows/ci.yml` exist for supply-chain reasons (issue #124, free tier). They will bit-rot and need occasional bumps:

- **`@railway/cli@<version>`** in the `Install Railway CLI` step of both `deploy-staging` and `deploy-production`. Failure mode is loud (deploy step fails with a CLI error). Bump by checking `npm view @railway/cli version` and updating both lines. Railway ships fast (~40 versions in 60 days as of 2026-05); pin "current" rather than chasing every release. Last bump: 57b2d8a (2026-05-12, pinned to 4.58.0).
- **Workflow-level `permissions:`** scoped to the minimum each workflow needs — `contents: read` for `ci.yml` and `external-api.yml`; `contents: read` plus `packages: read` for `charset-corpus-drift.yml` (which pulls `@wxyc/shared` from `npm.pkg.github.com`). Failure mode is silent (a job that needs e.g. `pull-requests: write` fails its API call but the workflow stays green). When adding a step that needs to comment on PRs, push tags, mint releases, etc., explicitly grant the scope at the job level — do not widen the workflow-level floor.

Run `actionlint .github/workflows/*.yml` locally before pushing workflow changes; it validates `permissions:` syntax, action-version pins, and shell-script blocks, and catches the silent-mistake class of errors above before CI does.

Both reusable-workflow refs (`WXYC/wxyc-shared/.../check-charset-corpus-drift.yml` in `charset-corpus-drift.yml`, and `WXYC/wxyc-etl/.../check-ci-marker-sync.yml` in `ci.yml`) are pinned to `@gha/v1` — the publisher's moving major tag with a documented [Tag Stability Policy](https://github.com/WXYC/wxyc-shared/blob/main/CLAUDE.md#tag-stability-policy-read-before-editing-githubworkflows). Non-breaking changes move the tag forward; breaking changes cut `gha/v2` and require a consumer-side bump. Do not re-point either ref at `@main` — that re-introduces silent breakage from publisher changes.
