#!/usr/bin/env bash
# Configure GitHub Actions secrets for stock-news-bot.
# Run from repo root after creating the GitHub remote.
#
# Usage:
#   export FINNHUB_API_KEY="your-finnhub-key"
#   export WHATSAPP_PHONE="+91xxxxxxxxxx"
#   export CALLMEBOT_API_KEY="your-callmebot-key"
#   ./scripts/configure_secrets.sh

set -euo pipefail

: "${FINNHUB_API_KEY:?Set FINNHUB_API_KEY}"
: "${WHATSAPP_PHONE:?Set WHATSAPP_PHONE}"
: "${CALLMEBOT_API_KEY:?Set CALLMEBOT_API_KEY}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is required. Install from https://cli.github.com/"
  exit 1
fi

gh secret set FINNHUB_API_KEY --body "$FINNHUB_API_KEY"
gh secret set WHATSAPP_PHONE --body "$WHATSAPP_PHONE"
gh secret set CALLMEBOT_API_KEY --body "$CALLMEBOT_API_KEY"

echo "All secrets configured successfully."
