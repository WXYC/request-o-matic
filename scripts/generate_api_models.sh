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
#      argv (visible in `ps`) or the script's own output. It is NOT proof
#      against `bash -x`, which traces the assignment and the printf; in Actions
#      the runner masks github.token in logs, so that exposure is a developer
#      tracing locally with their own PAT, to their own terminal.
#   2. Both variables are unset at the top of the script -- see the comment at
#      the capture; the placement above the source-resolution branch is
#      load-bearing.
#   3. A failed authenticated attempt retries once anonymously, unconditionally.
#      The motivating case is a stale token (GitHub 404s -- not 401s -- raw
#      requests carrying one, with no server-side anonymous fallback), but the
#      retry is not gated on that status: pre-fix anonymous behavior is the
#      floor in every environment. On a transient failure it costs one extra
#      attempt, which is the intended trade -- the anonymous per-IP budget is a
#      different bucket from the per-token one, so it can still succeed after
#      an authenticated 429.
#
# Unset-token runs are unchanged apart from a stderr note saying the download is
# anonymous (see the anonymous arm).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="$PROJECT_DIR/generated/api_models.py"

# The five streaming URL fields WXYC/wxyc-shared#428 annotated with
# `format: uri`, which datamodel-codegen maps to pydantic AnyUrl. Held at `str`
# below; see pin_streaming_url_fields_to_str().
STREAMING_URL_FIELDS=(
    spotify_url
    apple_music_url
    youtube_music_url
    bandcamp_url
    soundcloud_url
)

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
    # One definition of the URL/output pairing, called with or without the
    # header argument, so the three ways in can't drift apart.
    # --max-time/--retry mirror wxyc-shared's generate-python-models.sh pin for
    # this same download; curl's --retry also covers 429/5xx, honoring
    # Retry-After, which directly serves the #268 goal. --retry-max-time bounds
    # the whole ladder: without it, two arms x four attempts x 30s could burn
    # ~4 minutes of billed CI against a hard outage before failing anyway.
    _fetch_api_yaml() {
        curl -sSfL --max-time 30 --retry 3 --retry-max-time 60 "$@" "$API_YAML_URL" -o "$API_YAML"
    }
    if [[ -n "$AUTH_TOKEN" ]]; then
        echo "  Authenticated download: sending Authorization header (token value not logged)." >&2
        # -H @- reads the header from stdin, keeping the token off the process
        # argv (visible in `ps` on shared hosts). The retry below is
        # unconditional by design -- see header property 3.
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

# The WXYC/wxyc-shared#428 pin (ported from LML#1299 into request-o-matic#282),
# itself ported from wxyc-shared's canonical
# scripts/generate-python-models.sh. The canonical rationale lives there and is
# NOT restated here -- read it before changing either copy. The stake here:
# `format: uri` makes pydantic validate at DECODE time, so an unpinned regen
# turns any malformed streaming_links value arriving from LML into a hard
# decode failure on /request. Three `format: uri` fields are deliberately
# out of scope (the archive presigned-GET `url`, the OAuth device-flow
# `verification_uri`/`verification_uri_complete`): they carry no stored data and
# #428's decision does not cover them, which is why this is a name-keyed
# substitution over the five and not a codegen-wide type override -- both
# --field-constraints and --strict-types are file-wide once any override targets
# `format: uri`.
#
# Ported rather than migrated onto the canonical script: that migration replaces
# this fork wholesale (it has its own download/auth branch, #268, and the drift
# tracked in WXYC/wxyc-shared#369) and is a refactor with its own review
# surface. Migration stays available as a follow-up.
pin_streaming_url_fields_to_str() {
    local field
    for field in "${STREAMING_URL_FIELDS[@]}"; do
        sed -E "s/^([[:space:]]*${field}: )AnyUrl/\1str/" "$OUTPUT" > "$OUTPUT.pin-tmp"
        mv "$OUTPUT.pin-tmp" "$OUTPUT"
    done
}

# The substitution above is line-anchored on `fieldname: AnyUrl`, which is only
# ONE shape the generator can emit -- `Annotated[AnyUrl | None, Field(...)]`
# defeats it and every substitution silently no-ops while returning 0. That is
# the pin failing OPEN, so the post-condition cannot itself be another anchored
# regex: it parses the output and inspects each pinned field's annotation.
#
# It asserts what the annotation IS (an allowlist: `str` or `constr`, modulo None/Optional/
# Union/Annotated/Field wrappers), not what it is not. LML#1312 review: a
# denylist for the literal token `AnyUrl` -- which is what wxyc-shared's
# canonical copy does today -- fails open on one indirection this generator
# demonstrably produces. Give a field a named `$ref` schema and it generates as
# `class StreamingUrl(RootModel[AnyUrl])` plus `field: StreamingUrl | None`; the
# sed does not match, the token `AnyUrl` is absent from the field's own
# annotation, and all five ship validating URLs at decode time anyway. That is
# not hypothetical: this regen landed `class Url(RootModel[...])` for exactly
# that reason, and AlbumMetadata's own docstring says these five fields "will be
# migrated to use `$ref` in a future version". Anything outside
# `str`/`constr` decodes differently and wants a human, not a silent pass.
# `constr` is allowed (LML#1476): upstream gave `bandcamp_url` a `maxLength` on
# unrelated intake schemas, and a constrained string still decodes as `str`.
#
# It also asserts every pinned field was FOUND. `offenders`-only logic is
# vacuous under a rename: upstream renames one of the five, the sed no-ops, the
# walk yields nothing, and the run exits 0 with four of five pinned.
#
# This is a deliberate divergence from wxyc-shared's canonical script (a third
# known one, after WXYC/wxyc-shared#369 and LML's download/auth branch); the
# same hardening is worth taking upstream so the copies reconverge.
# tests/unit/test_generate_api_models_pin.py drives every failure mode.
verify_streaming_url_fields_pinned() {
    local python_cmd=()
    if command -v python3 &> /dev/null; then
        python_cmd=(python3)
    elif command -v python &> /dev/null; then
        python_cmd=(python)
    elif command -v uv &> /dev/null; then
        python_cmd=(uv run --no-project --python 3.12 python3)
    else
        echo "Error: no Python interpreter found to verify the #428 pin (need python3, python, or uv)." >&2
        exit 1
    fi

    STREAMING_URL_FIELDS_CSV="$(IFS=,; echo "${STREAMING_URL_FIELDS[*]}")" OUTPUT="$OUTPUT" \
        "${python_cmd[@]}" -c '
import ast
import os
import sys

output_path = os.environ["OUTPUT"]
fields = set(os.environ["STREAMING_URL_FIELDS_CSV"].split(","))

with open(output_path, "r", encoding="utf-8") as f:
    source = f.read()

# Wrappers that may surround the pinned type without changing what it decodes as.
WRAPPERS = {"None", "Optional", "Union", "Annotated", "Field"}
# Names that may carry the type itself: a constr(...) is still a plain str on
# decode, it only adds a length/pattern check (LML#1476).
PLAIN_STR_NAMES = {"str", "constr"}

tree = ast.parse(source, filename=output_path)
offenders = []
seen = set()
for node in ast.walk(tree):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.target.id in fields:
            seen.add(node.target.id)
            annotation_src = ast.unparse(node.annotation)
            referenced = {
                n.id for n in ast.walk(node.annotation) if isinstance(n, ast.Name)
            } - WRAPPERS
            if not referenced or not referenced <= PLAIN_STR_NAMES:
                offenders.append(f"{node.target.id} (line {node.lineno}): {annotation_src}")

if offenders:
    sys.stderr.write(
        "#428 pin did not apply -- the following streaming URL fields are not plain "
        "`str` in their generated annotation (pin_streaming_url_fields_to_str "
        "silently no-op'\''d, most likely because the generator emitted a shape its "
        "sed pattern does not match -- e.g. an Annotated[...] or a named RootModel "
        "wrapper):\n"
    )
    for offender in offenders:
        sys.stderr.write(f"  - {offender}\n")
    sys.exit(1)

missing = sorted(fields - seen)
if missing:
    sys.stderr.write(
        "#428 pin did not apply -- the following streaming URL fields were not found "
        "in the generated output at all, so the pin covered fewer fields than it "
        "claims (most likely an upstream rename; update STREAMING_URL_FIELDS):\n"
    )
    for field in missing:
        sys.stderr.write(f"  - {field}\n")
    sys.exit(1)
'
}

echo "Pinning streaming URL fields to str (#428)..."
pin_streaming_url_fields_to_str
verify_streaming_url_fields_pinned

# Format with ruff
echo "Formatting generated code..."
"$RUFF" format "$OUTPUT" 2>/dev/null || true
"$RUFF" check --fix "$OUTPUT" 2>/dev/null || true

echo "Generated: $OUTPUT"
