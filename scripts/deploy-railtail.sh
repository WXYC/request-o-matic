#!/bin/bash
set -eo pipefail

# Deploy Railtail (Tailscale-to-Railway TCP proxy) and wire it up to
# request-o-matic for Discogs cache connectivity.
#
# Usage:
#   ./scripts/deploy-railtail.sh --environment staging
#   ./scripts/deploy-railtail.sh --environment production

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
RAILTAIL_CLONE_DIR="${REPO_DIR}/.railtail"
RAILTAIL_REPO="https://github.com/half0wl/railtail.git"

SERVICE_NAME="railtail"
LISTEN_PORT="5432"
DEFAULT_TARGET_ADDR=""
DEFAULT_DB_USER=""
DEFAULT_DB_NAME="discogs"

ENVIRONMENT=""
TS_AUTH_KEY=""
TARGET_ADDR=""
DB_USER=""
DB_NAME=""

# --- Logging ---

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

error() {
    log "ERROR: $1"
    exit 1
}

# --- Input helpers ---

prompt() {
    local message="$1"
    local default="$2"
    local var_name="$3"

    if [[ -n "$default" ]]; then
        read -rp "$message [$default]: " value
        value="${value:-$default}"
    else
        read -rp "$message: " value
    fi

    eval "$var_name=\$value"
}

# --- Prerequisite checks ---

check_prereqs() {
    log "Checking prerequisites..."

    if ! command -v railway &>/dev/null; then
        error "Railway CLI not found. Install with: brew install railway"
    fi

    if ! command -v git &>/dev/null; then
        error "git not found"
    fi

    log "Prerequisites OK"
}

# --- Railway environment ---

select_railway_environment() {
    local token_var

    if [[ "$ENVIRONMENT" == "staging" ]]; then
        token_var="RAILWAY_TOKEN_STAGING"
    else
        token_var="RAILWAY_TOKEN_PRODUCTION"
    fi

    if [[ -z "${!token_var}" ]]; then
        error "$token_var is not set. Export it before running this script."
    fi

    export RAILWAY_TOKEN="${!token_var}"
    log "Using Railway token from $token_var"
}

# --- Railtail service ---

set_up_railtail_service() {
    log "Checking for existing railtail service..."

    if railway service link "$SERVICE_NAME" 2>/dev/null; then
        log "Service '$SERVICE_NAME' already exists, linked"
    else
        log "Service '$SERVICE_NAME' not found -- creating it"
        railway add --service "$SERVICE_NAME"
        railway service link "$SERVICE_NAME"
        log "Created and linked service '$SERVICE_NAME'"
    fi
}

# --- Volume ---

set_up_volume() {
    log "Checking for existing volume..."

    local volumes
    volumes=$(railway volume list --service "$SERVICE_NAME" 2>/dev/null || true)

    if echo "$volumes" | grep -q "/railtail"; then
        log "Volume at /railtail already exists"

        echo ""
        echo "If Tailscale auth is failing due to stale state, you may need"
        echo "to recreate the volume. This will delete existing Tailscale state."
        read -rp "Recreate volume? (y/N): " recreate
        if [[ "$recreate" =~ ^[Yy]$ ]]; then
            log "Detaching existing volume..."
            railway volume delete --service "$SERVICE_NAME" --yes 2>/dev/null || true
            log "Creating fresh volume at /railtail..."
            railway volume add --service "$SERVICE_NAME" --mount-path /railtail
            log "Volume recreated"
        fi
    else
        log "Creating volume at /railtail..."
        railway volume add --service "$SERVICE_NAME" --mount-path /railtail
        log "Volume created"
    fi
}

# --- Environment variables ---

set_railtail_variables() {
    local ts_hostname="${SERVICE_NAME}-${ENVIRONMENT}"

    log "Setting environment variables on railtail service..."
    railway variable set \
        "TS_AUTH_KEY=$TS_AUTH_KEY" \
        "TARGET_ADDR=$TARGET_ADDR" \
        "LISTEN_PORT=$LISTEN_PORT" \
        "TS_HOSTNAME=$ts_hostname" \
        "TS_STATEDIR_PATH=/railtail" \
        --service "$SERVICE_NAME" \
        --skip-deploys

    log "Variables set (deployment skipped for now)"
}

# --- Deploy ---

deploy_railtail() {
    log "Preparing railtail source..."

    if [[ -d "$RAILTAIL_CLONE_DIR" ]]; then
        log "Updating existing clone at $RAILTAIL_CLONE_DIR"
        git -C "$RAILTAIL_CLONE_DIR" pull --ff-only
    else
        log "Cloning railtail repo..."
        git clone "$RAILTAIL_REPO" "$RAILTAIL_CLONE_DIR"
    fi

    log "Deploying railtail from source (GHCR image not available)..."
    (cd "$RAILTAIL_CLONE_DIR" && railway up --detach --service "$SERVICE_NAME")

    log "Deployment started"
}

# --- Wire up request-o-matic ---

wire_database_url() {
    local railtail_host="${SERVICE_NAME}.railway.internal"
    local database_url="postgresql://${DB_USER}@${railtail_host}:${LISTEN_PORT}/${DB_NAME}"

    log "Setting DATABASE_URL_DISCOGS on request-o-matic..."
    railway variable set \
        "DATABASE_URL_DISCOGS=$database_url" \
        --service request-o-matic \
        --skip-deploys

    log "DATABASE_URL_DISCOGS=${database_url}"
    log "Set on request-o-matic (redeploy to pick up)"
}

# --- Verify ---

verify() {
    log "Waiting for railtail to start..."
    sleep 10

    log "Checking recent railtail logs..."
    local logs
    logs=$(railway logs --lines 30 --service "$SERVICE_NAME" 2>/dev/null || true)

    if echo "$logs" | grep -qi "tailscale"; then
        log "Tailscale activity detected in logs"
    else
        log "WARNING: No Tailscale activity in logs yet. It may still be starting."
    fi

    if echo "$logs" | grep -qi "listening"; then
        log "Railtail is listening for connections"
    else
        log "WARNING: Railtail may not be listening yet. Check logs with: railway logs"
    fi

    echo ""
    echo "$logs"
}

# --- Main ---

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --environment)
                ENVIRONMENT="$2"
                shift 2
                ;;
            -h|--help)
                echo "Usage: $0 --environment <staging|production>"
                echo ""
                echo "Deploys Railtail (Tailscale TCP proxy) on Railway and wires it"
                echo "to request-o-matic for Discogs cache connectivity."
                echo ""
                echo "Options:"
                echo "  --environment   Railway environment (staging or production)"
                echo "  -h, --help      Show this help message"
                echo ""
                echo "Environment variables:"
                echo "  RAILWAY_TOKEN_STAGING      Token for staging environment"
                echo "  RAILWAY_TOKEN_PRODUCTION   Token for production environment"
                exit 0
                ;;
            *)
                error "Unknown option: $1. Use --help for usage."
                ;;
        esac
    done

    if [[ -z "$ENVIRONMENT" ]]; then
        error "Missing --environment. Use --help for usage."
    fi

    if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
        error "Environment must be 'staging' or 'production', got '$ENVIRONMENT'"
    fi
}

gather_inputs() {
    echo ""
    echo "=== Railtail Deployment ($ENVIRONMENT) ==="
    echo ""

    prompt "Tailscale auth key (tskey-auth-...)" "" TS_AUTH_KEY

    if [[ ! "$TS_AUTH_KEY" =~ ^tskey-auth- ]]; then
        error "Auth key must start with 'tskey-auth-'. OAuth client secrets (tskey-client-...) don't work with railtail."
    fi

    prompt "Target PostgreSQL address (host:port)" "$DEFAULT_TARGET_ADDR" TARGET_ADDR
    prompt "Database user" "$DEFAULT_DB_USER" DB_USER
    prompt "Database name" "$DEFAULT_DB_NAME" DB_NAME

    echo ""
    echo "Configuration:"
    echo "  Environment:  $ENVIRONMENT"
    echo "  Target:       $TARGET_ADDR"
    echo "  DB user:      $DB_USER"
    echo "  DB name:      $DB_NAME"
    echo "  Listen port:  $LISTEN_PORT"
    echo ""
    read -rp "Proceed? (Y/n): " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
        log "Aborted by user"
        exit 0
    fi
}

main() {
    parse_args "$@"
    check_prereqs
    select_railway_environment
    gather_inputs

    set_up_railtail_service
    set_up_volume
    set_railtail_variables
    deploy_railtail
    wire_database_url
    verify

    echo ""
    echo "=== Deployment Complete ==="
    echo ""
    echo "Railtail is deploying on Railway ($ENVIRONMENT)."
    echo ""
    echo "Next steps:"
    echo "  1. Verify railtail joined the tailnet: railway logs"
    echo "  2. Redeploy request-o-matic to pick up DATABASE_URL_DISCOGS"
    echo "  3. Check the /health endpoint for cache pool status"
    echo ""
    echo "Useful commands:"
    echo "  railway logs                    # railtail logs"
    echo "  railway service request-o-matic # switch to main service"
    echo "  railway logs                    # main service logs"
}

main "$@"
