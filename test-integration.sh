#!/bin/bash
# Run integration tests using staging environment variables from Railway
set -e

export RAILWAY_TOKEN="${RAILWAY_TOKEN_STAGING}"

if [ -z "$RAILWAY_TOKEN" ]; then
  echo "Error: RAILWAY_TOKEN_STAGING is not set"
  echo "Set it in your shell profile (~/.zshrc) or export it before running this script"
  exit 1
fi

railway run pytest tests/integration/ "$@"
