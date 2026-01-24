#!/bin/bash
set -eo pipefail

LOG_FILE="$HOME/Library/Logs/request-parser-etl.log"
REPO_DIR="/Users/jake/Developer/request-parser"
SLACK_WEBHOOK_URL="${SLACK_MONITORING_WEBHOOK:-}"
NOTIFY_ENABLED=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --notify)
            NOTIFY_ENABLED=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--notify]"
            exit 1
            ;;
    esac
done

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$msg" >> "$LOG_FILE"
    echo "$msg"
}

notify_error() {
    local message="$1"
    log "ERROR: $message"

    if [[ "$NOTIFY_ENABLED" == "true" && -n "$SLACK_WEBHOOK_URL" ]]; then
        curl -s -X POST "$SLACK_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "{\"text\":\":warning: *Library ETL Failed*\n$message\"}" \
            >> "$LOG_FILE" 2>&1 || true
    fi
}

cd "$REPO_DIR"

# Load environment variables from .env if it exists
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

log "Starting library sync"

# Run ETL, capturing output for error reporting
ETL_OUTPUT=$(mktemp)
if ! venv/bin/python scripts/export_to_sqlite.py 2>&1 | tee "$ETL_OUTPUT"; then
    # Extract the final error line (last non-empty line not starting with whitespace)
    ERROR_DETAILS=$(grep -v '^[[:space:]]' "$ETL_OUTPUT" | grep -v '^$' | tail -1 | sed 's/"/\\"/g')
    cat "$ETL_OUTPUT" >> "$LOG_FILE"
    rm -f "$ETL_OUTPUT"
    notify_error "ETL script failed: $ERROR_DETAILS"
    exit 1
fi
cat "$ETL_OUTPUT" >> "$LOG_FILE"
rm -f "$ETL_OUTPUT"

# Check if database changed
if git diff --quiet library.db; then
    log "No changes to library.db"
    exit 0
fi

# Commit and push
log "Changes detected, committing..."
git add library.db
git commit -m "Update library catalog $(date +%Y-%m-%d)"

PUSH_OUTPUT=$(mktemp)
if ! git push origin main >> "$PUSH_OUTPUT" 2>&1; then
    # Extract the final error line
    ERROR_DETAILS=$(grep -v '^[[:space:]]' "$PUSH_OUTPUT" | grep -v '^$' | tail -1 | sed 's/"/\\"/g')
    cat "$PUSH_OUTPUT" >> "$LOG_FILE"
    rm -f "$PUSH_OUTPUT"
    notify_error "Git push failed: $ERROR_DETAILS"
    exit 1
fi
cat "$PUSH_OUTPUT" >> "$LOG_FILE"
rm -f "$PUSH_OUTPUT"

log "Pushed to staging successfully"
