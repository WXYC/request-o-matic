# Deployment

- Hosted on Railway
- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
- Use `railway` CLI for status/logs (requires TTY for some commands)

## CI pin maintenance

Two pins in `.github/workflows/ci.yml` exist for supply-chain reasons (issue #124, free tier). They will bit-rot and need occasional bumps:

- **`@railway/cli@<version>`** in the `Install Railway CLI` step of both `deploy-staging` and `deploy-production`. Failure mode is loud (deploy step fails with a CLI error). Bump by checking `npm view @railway/cli version` and updating both lines. Railway ships fast (~40 versions in 60 days as of 2026-05); pin "current" rather than chasing every release. Last bump: 57b2d8a (2026-05-12, pinned to 4.58.0).
- **Workflow-level `permissions:`** scoped to the minimum each workflow needs — `contents: read` for `ci.yml` and `external-api.yml`; `contents: read` plus `packages: read` for `charset-corpus-drift.yml` (which pulls `@wxyc/shared` from `npm.pkg.github.com`). Failure mode is silent (a job that needs e.g. `pull-requests: write` fails its API call but the workflow stays green). When adding a step that needs to comment on PRs, push tags, mint releases, etc., explicitly grant the scope at the job level — do not widen the workflow-level floor.

Run `actionlint .github/workflows/*.yml` locally before pushing workflow changes; it validates `permissions:` syntax, action-version pins, and shell-script blocks, and catches the silent-mistake class of errors above before CI does.

Both reusable-workflow refs (`WXYC/wxyc-shared/.../check-charset-corpus-drift.yml` in `charset-corpus-drift.yml`, and `WXYC/wxyc-etl/.../check-ci-marker-sync.yml` in `ci.yml`) are pinned to `@gha/v1` — the publisher's moving major tag with a documented [Tag Stability Policy](https://github.com/WXYC/wxyc-shared/blob/main/CLAUDE.md#tag-stability-policy-read-before-editing-githubworkflows). Non-breaking changes move the tag forward; breaking changes cut `gha/v2` and require a consumer-side bump. Do not re-point either ref at `@main` — that re-introduces silent breakage from publisher changes.
