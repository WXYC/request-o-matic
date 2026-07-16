#!/usr/bin/env bash
#
# install-lookup.sh — install the WXYC `lookup` diagnostic CLI as a global command.
#
# Builds a small httpx-only virtualenv and drops a `lookup` launcher on your
# PATH that runs this repo's scripts/lookup.py through it. The launcher points
# back at this checkout, so it must stay in place — moving or deleting the repo
# (including pruning the git worktree you installed from) breaks `lookup`.
# Run with --help for full usage and environment overrides.

set -euo pipefail

# --- Resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOOKUP_SCRIPT="$SCRIPT_DIR/lookup.py"

# --- Configuration (overridable via environment) ----------------------------
PYTHON="${PYTHON:-python3}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LOOKUP_VENV="${LOOKUP_VENV:-$XDG_DATA_HOME/wxyc/lookup-venv}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/lookup"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

# --- Logging helpers --------------------------------------------------------
if [ -t 1 ]; then
    C_BLUE=$'\033[1;34m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
    C_GREEN=$'\033[1;32m'; C_RESET=$'\033[0m'
else
    C_BLUE=''; C_YELLOW=''; C_RED=''; C_GREEN=''; C_RESET=''
fi
info() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

# POSIX single-quote a string so it embeds literally in the generated /bin/sh
# launcher — a $, backtick, or quote in the path is then not re-evaluated when
# `lookup` runs. Wraps in single quotes, escaping any embedded single quote.
_sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

trap 'err "install failed at line $LINENO"; exit 1' ERR

usage() {
    cat <<'EOF'
install-lookup.sh — install the WXYC `lookup` diagnostic CLI as a global command.

The lookup utility (scripts/lookup.py) is a thin HTTP client whose only runtime
dependency is httpx. This installer builds a small isolated virtualenv with just
httpx and drops a `lookup` launcher on your PATH that runs scripts/lookup.py
through it — it does not pull in the FastAPI/Groq service dependency tree.

The launcher points back at this checkout, so edits to scripts/lookup.py take
effect immediately, but the repo must stay in place: moving or deleting it (or
pruning the git worktree you installed from) breaks `lookup`.

Usage:
  scripts/install-lookup.sh              # install or update the `lookup` command
  scripts/install-lookup.sh --uninstall  # remove the launcher and its venv
  scripts/install-lookup.sh --help

Environment overrides:
  PYTHON        Python interpreter used to build the venv  (default: python3)
  LOOKUP_VENV   Where the isolated venv is created         (default: $XDG_DATA_HOME/wxyc/lookup-venv)
  BIN_DIR       Directory the launcher is installed into   (default: ~/.local/bin)
EOF
}

uninstall() {
    local removed=0
    # -L also catches a dangling symlink, which -e misses.
    if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
        rm -f "$LAUNCHER"
        ok "Removed launcher $LAUNCHER"
        removed=1
    fi
    if [ -d "$LOOKUP_VENV" ]; then
        rm -rf "$LOOKUP_VENV"
        ok "Removed venv $LOOKUP_VENV"
        removed=1
        # Drop the parent (e.g. .../wxyc) if uninstalling left it empty.
        rmdir "$(dirname "$LOOKUP_VENV")" 2>/dev/null || true
    fi
    if [ "$removed" -eq 0 ]; then
        info "Nothing to uninstall (no launcher or venv found)."
    fi
}

install() {
    if [ ! -f "$LOOKUP_SCRIPT" ]; then
        err "Cannot find lookup CLI at $LOOKUP_SCRIPT"
        exit 1
    fi

    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        err "Python interpreter '$PYTHON' not found. Set PYTHON=/path/to/python3 and retry."
        exit 1
    fi
    if ! "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)"; then
        err "Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR required, found $("$PYTHON" -V 2>&1)."
        exit 1
    fi

    if [ -x "$LOOKUP_VENV/bin/python" ]; then
        info "Reusing existing virtualenv at $LOOKUP_VENV"
    else
        info "Creating virtualenv at $LOOKUP_VENV"
        "$PYTHON" -m venv "$LOOKUP_VENV"
    fi
    local venv_py="$LOOKUP_VENV/bin/python"

    # A venv without pip (e.g. Debian/Ubuntu missing python3-venv, so ensurepip
    # never ran) can't install httpx. Guide the user rather than delete the dir:
    # LOOKUP_VENV may point at a venv they supplied, and this script must never
    # remove data it did not create.
    if ! "$venv_py" -m pip --version >/dev/null 2>&1; then
        err "The virtualenv at $LOOKUP_VENV has no pip; cannot install httpx."
        err "Your '$PYTHON' may lack the venv/ensurepip module (Debian/Ubuntu: sudo apt install python3-venv)."
        err "Then remove $LOOKUP_VENV, or point LOOKUP_VENV at a fresh path, and re-run."
        exit 1
    fi

    info "Installing httpx into the virtualenv"
    "$venv_py" -m pip install --quiet --disable-pip-version-check 'httpx>=0.25'

    info "Writing launcher to $LAUNCHER"
    mkdir -p "$BIN_DIR"
    # Write to a temp file and atomically rename into place: this replaces any
    # pre-existing launcher (or symlink) without truncating through it, and is
    # safe against a concurrent install. The baked paths are single-quoted so
    # the generated /bin/sh does not re-evaluate a $, backtick, or quote in them.
    local tmp_launcher="$LAUNCHER.tmp.$$"
    cat > "$tmp_launcher" <<EOF
#!/bin/sh
# Auto-generated by request-o-matic scripts/install-lookup.sh — do not edit.
# Runs the WXYC lookup CLI from $REPO_ROOT via an isolated httpx-only venv.
exec $(_sq "$venv_py") $(_sq "$LOOKUP_SCRIPT") "\$@"
EOF
    chmod +x "$tmp_launcher"
    mv -f "$tmp_launcher" "$LAUNCHER"

    ok "Installed 'lookup' -> $LOOKUP_SCRIPT"
    echo
    info "Try it:"
    echo "    lookup \"play la paradoja by juana molina\""
    echo "    lookup --staging \"jessica pratt on your own love again\""

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            echo
            warn "$BIN_DIR is not on your PATH."
            warn "Add this line to your shell profile (~/.zshrc or ~/.bashrc):"
            warn "    export PATH=\"$BIN_DIR:\$PATH\""
            ;;
    esac
}

main() {
    case "${1:-}" in
        -h|--help) usage; exit 0 ;;
        --uninstall) uninstall; exit 0 ;;
        "") install ;;
        *) err "unknown argument: $1"; echo; usage; exit 2 ;;
    esac
}

main "$@"
