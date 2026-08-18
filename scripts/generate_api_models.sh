#!/usr/bin/env bash
# Generate Python Pydantic v2 models from the wxyc-shared api.yaml OpenAPI spec.
#
# Looks for api.yaml in a sibling wxyc-shared directory first, then falls back
# to downloading from GitHub. The generated file is committed to git so that
# normal CI jobs don't need the codegen toolchain.
#
# Usage:
#   bash scripts/generate_api_models.sh
#
# CANONICAL RATIONALE for the token flow (#268; ported from
# WXYC/library-metadata-lookup#1205) -- ci.yml points here rather than
# restating it. When GH_TOKEN or GITHUB_TOKEN is set (GH_TOKEN wins, matching
# gh's own precedence -- `gh help environment`), the GitHub download
# authenticates via a Bearer Authorization header: anonymous
# raw.githubusercontent.com requests share a per-IP rate budget across the
# Actions runner pool and intermittently 429, while authenticated ones get a
# per-token budget. Three properties, each pinned by a test in
# tests/unit/test_generate_api_models_auth.py:
#
#   1. The header rides curl's stdin (-H @-), so the token value never reaches
#      argv (visible in `ps`) or the log.
#   2. Both variables are unset at the top of the script -- above the
#      source-resolution branch, so no child process inherits the token on
#      EITHER arm. The sibling-checkout arm never downloads, so a scrub placed
#      inside the download branch would miss the default local invocation.
#   3. A failed authenticated attempt retries once anonymously, unconditionally.
#      The motivating case is a stale token (GitHub 404s -- not 401s -- raw
#      requests carrying one, with no server-side anonymous fallback), but the
#      retry is not gated on that status: pre-fix anonymous behavior is the
#      floor in every environment. On a transient failure it costs one extra
#      attempt, which is the intended trade -- the anonymous per-IP budget is a
#      different bucket from the per-token one, so it can still succeed after
#      an authenticated 429.
#
# Unset-token runs are unchanged, apart from a stderr note saying the download
# is anonymous -- so losing the CI token surfaces as a visible regression
# rather than as a return of the original intermittent 429.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_DIR/generated/api_models.py"

# Capture the ambient GitHub token (#268; GH_TOKEN wins) and immediately drop
# both names from the environment. Done HERE, before the source resolution
# below, because the scrub is a property of the whole script rather than of the
# download arm: the sibling-checkout arm never downloads, and leaving the unset
# inside the download branch would hand the token to datamodel-codegen, ruff,
# and their whole dependency tree on the default local invocation. Only the
# download needs the value, and it travels from this variable, never the
# environment.
AUTH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
unset GITHUB_TOKEN GH_TOKEN

# Resolve api.yaml source. Inside a git worktree, the sibling layout is rooted
# at the main repo, not at the worktree path — use --git-common-dir to find it.
SIBLING_PATH="$PROJECT_DIR/../wxyc-shared/api.yaml"
if MAIN_GIT_DIR="$(cd "$PROJECT_DIR" && git rev-parse --git-common-dir 2>/dev/null)"; then
    if [[ "$MAIN_GIT_DIR" != /* ]]; then
        MAIN_GIT_DIR="$PROJECT_DIR/$MAIN_GIT_DIR"
    fi
    MAIN_REPO_ROOT="$(cd "$MAIN_GIT_DIR/.." && pwd)"
    SIBLING_PATH="$MAIN_REPO_ROOT/../wxyc-shared/api.yaml"
fi

if [[ -f "$SIBLING_PATH" ]]; then
    API_YAML="$SIBLING_PATH"
    echo "Using local api.yaml: $API_YAML"
else
    API_YAML="$(mktemp)"
    trap 'rm -f "$API_YAML"' EXIT
    echo "Downloading api.yaml from GitHub..."
    API_YAML_URL="https://raw.githubusercontent.com/WXYC/wxyc-shared/main/api.yaml"
    # --max-time/--retry mirror wxyc-shared's generate-python-models.sh pin for
    # this same download; curl's --retry also covers 429/5xx, honoring
    # Retry-After, which directly serves the #268 goal.
    CURL_OPTS=(-sSfL --max-time 30 --retry 3)
    # One definition of the URL/output pairing, called with or without the
    # header argument, so the ways in can't drift apart.
    _fetch_api_yaml() { curl "${CURL_OPTS[@]}" "$@" "$API_YAML_URL" -o "$API_YAML"; }
    if [[ -n "$AUTH_TOKEN" ]]; then
        echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
        # -H @- reads the header from stdin, keeping the token off the process
        # argv (visible in `ps` on shared hosts). The retry below is
        # deliberately UNconditional rather than gated on the stale-token 404
        # described in the header: pre-#268 anonymous behavior is the floor in
        # every environment.
        if ! printf 'Authorization: Bearer %s\n' "$AUTH_TOKEN" | _fetch_api_yaml -H @-; then
            echo "  Authenticated download failed; retrying anonymously (is the ambient token stale?)..." >&2
            _fetch_api_yaml
        fi
    else
        # Say so: without this line a dropped `GITHUB_TOKEN:` in the workflow
        # step silently reverts CI to the anonymous path #268 exists to leave,
        # and the regression presents as the original intermittent 429.
        echo "  No GH_TOKEN/GITHUB_TOKEN set: downloading anonymously (shared per-IP rate budget)." >&2
        _fetch_api_yaml
    fi
    echo "Downloaded to $API_YAML"
fi

# Ensure output directory exists
mkdir -p "$(dirname "$OUTPUT")"

# Locate tools: prefer venv, fall back to PATH
CODEGEN="${PROJECT_DIR}/.venv/bin/datamodel-codegen"
if [[ ! -x "$CODEGEN" ]]; then
    CODEGEN="$(command -v datamodel-codegen 2>/dev/null || true)"
    if [[ -z "$CODEGEN" ]]; then
        echo "Error: datamodel-codegen not found. Install with: uv pip install 'datamodel-code-generator[http]'" >&2
        exit 1
    fi
fi

RUFF="${PROJECT_DIR}/.venv/bin/ruff"
if [[ ! -x "$RUFF" ]]; then
    RUFF="$(command -v ruff 2>/dev/null || echo ruff)"
fi

# Generate models
echo "Generating Python models..."
"$CODEGEN" \
    --input "$API_YAML" \
    --input-file-type openapi \
    --output "$OUTPUT" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.12 \
    --use-standard-collections \
    --use-union-operator \
    --disable-timestamp \
    --custom-file-header "# Generated from wxyc-shared/api.yaml -- do not edit manually.
# Regenerate with: bash scripts/generate_api_models.sh"

# Format with ruff
echo "Formatting generated code..."
"$RUFF" format "$OUTPUT" 2>/dev/null || true
"$RUFF" check --fix "$OUTPUT" 2>/dev/null || true

echo "Generated: $OUTPUT"
