#!/usr/bin/env bash
# Configure GitHub Actions secrets for stock-news-bot.
# Run from repo root after creating the GitHub remote.
#
# Usage:
#   export FINNHUB_API_KEY="your-finnhub-key"
#   export WHATSAPP_PHONE="+91xxxxxxxxxx"
#   export CALLMEBOT_API_KEY="your-callmebot-key"
#   export BREVO_API_KEY="your-brevo-api-key"
#   export BREVO_LIST_ID="2"
#   export BREVO_SUBSCRIBE_FORM_URL="https://my.brevo.com/subscribe/your-form-id"
#   export EMAIL_FROM="digest@yourdomain.com"
#   export EMAIL_FROM_NAME="Stock News Bot"   # optional
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

if [ -n "${BREVO_API_KEY:-}" ]; then
  : "${EMAIL_FROM:?Set EMAIL_FROM when BREVO_API_KEY is set}"
  gh secret set BREVO_API_KEY --body "$BREVO_API_KEY"
  gh secret set EMAIL_FROM --body "$EMAIL_FROM"
  if [ -n "${BREVO_LIST_ID:-}" ]; then
    gh secret set BREVO_LIST_ID --body "$BREVO_LIST_ID"
  fi
  if [ -n "${BREVO_SUBSCRIBE_FORM_URL:-}" ]; then
    gh secret set BREVO_SUBSCRIBE_FORM_URL --body "$BREVO_SUBSCRIBE_FORM_URL"
  fi
  if [ -n "${EMAIL_TO:-}" ]; then
    gh secret set EMAIL_TO --body "$EMAIL_TO"
  fi
  if [ -n "${EMAIL_FROM_NAME:-}" ]; then
    gh secret set EMAIL_FROM_NAME --body "$EMAIL_FROM_NAME"
  fi
  echo "Email secrets configured."
else
  echo "Email secrets skipped (set BREVO_API_KEY, EMAIL_FROM, and BREVO_LIST_ID to enable)."
fi

echo "All secrets configured successfully."
