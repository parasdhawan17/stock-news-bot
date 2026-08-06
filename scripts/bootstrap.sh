#!/usr/bin/env bash
# One-time bootstrap: init git, push to GitHub, set secrets, run first workflow.
#
# Prerequisites:
#   1. CallMeBot authorized (send "I allow callmebot to send me messages" to +34 644 44 71 67)
#   2. Finnhub API key from https://finnhub.io/
#   3. GitHub CLI installed and authenticated (gh auth login)
#
# Usage:
#   export FINNHUB_API_KEY="your-finnhub-key"
#   export WHATSAPP_PHONE="+91xxxxxxxxxx"
#   export CALLMEBOT_API_KEY="your-callmebot-key"
#   ./scripts/bootstrap.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

: "${FINNHUB_API_KEY:?Set FINNHUB_API_KEY}"
: "${WHATSAPP_PHONE:?Set WHATSAPP_PHONE}"
: "${CALLMEBOT_API_KEY:?Set CALLMEBOT_API_KEY}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is required. Install from https://cli.github.com/"
  exit 1
fi

if [ ! -d .git ]; then
  git init -b main
  git add .
  git commit -m "Initial stock news bot with GitHub Actions and CallMeBot delivery."
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  gh repo create stock-news-bot --public --source=. --remote=origin --push
else
  git push -u origin main
fi

./scripts/configure_secrets.sh

echo ""
echo "Triggering test workflow run..."
gh workflow run daily-stock-news.yml

echo ""
echo "Bootstrap complete. Check Actions tab for run status:"
gh repo view --web 2>/dev/null || true
