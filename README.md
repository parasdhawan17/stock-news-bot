# Stock News Bot

Daily US stock news digest delivered to WhatsApp via GitHub Actions.

**Cost: $0** — Finnhub free API, CallMeBot free WhatsApp delivery, GitHub Actions on a public repo.

## How it works

1. GitHub Actions runs daily at 8:00 AM IST (or manually via **Run workflow**).
2. The script reads tickers from `config/tickers.json`.
3. Finnhub company-news API fetches the last 24 hours of headlines per ticker.
4. A formatted message is sent to your WhatsApp via CallMeBot.

## Project structure

```
stock-news-bot/
├── .github/workflows/daily-stock-news.yml
├── config/tickers.json
├── scripts/send_stock_news.py
├── requirements.txt
└── README.md
```

## Setup

### 1. Finnhub API key

1. Sign up at [finnhub.io](https://finnhub.io/).
2. Copy your API key from the dashboard.

### 2. CallMeBot WhatsApp authorization

1. Save **+34 644 44 71 67** as a WhatsApp contact (e.g. "CallMeBot").
2. Send it: `I allow callmebot to send me messages`
3. It replies with your personal API key — save this for the next step.

### 3. GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|--------|-------|
| `FINNHUB_API_KEY` | Your Finnhub API key |
| `WHATSAPP_PHONE` | Your number with country code, e.g. `+919876543210` |
| `CALLMEBOT_API_KEY` | API key from CallMeBot |

### 4. Push to GitHub and configure secrets

Use a **public** repo for unlimited free GitHub Actions minutes.

**Option A — automated (recommended):**

```bash
cd /Users/anisharajput/Documents/Github/stock-news-bot
export FINNHUB_API_KEY="your-finnhub-key"
export WHATSAPP_PHONE="+91xxxxxxxxxx"
export CALLMEBOT_API_KEY="your-callmebot-key"
./scripts/bootstrap.sh
```

This script pushes to GitHub, sets all secrets, and triggers a test workflow run.

**Option B — manual:**

```bash
cd /Users/anisharajput/Documents/Github/stock-news-bot
git push -u origin main   # after creating the repo on GitHub
./scripts/configure_secrets.sh
```

Or add secrets in the GitHub UI: **Settings → Secrets and variables → Actions**.

### 5. Test

1. Open **Actions** → **Daily Stock News** → **Run workflow**.
2. Check the run logs for `WhatsApp message sent`.
3. Confirm the message on your phone.

## Customize tickers

Edit `config/tickers.json`:

```json
{
  "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
}
```

US symbols use plain tickers. Keep the list to ~10 tickers to stay within message length limits.

## Local testing

Copy `.env.example` to `.env` and fill in your keys, then:

```bash
pip install -r requirements.txt
set -a && source .env && set +a
python scripts/send_stock_news.py
```

## Schedule

The workflow cron is `30 2 * * *` (UTC) = **8:00 AM IST**.

GitHub cron can run a few minutes late on the free tier — fine for a morning digest.

## Notes

- CallMeBot is unofficial and for personal use only.
- Scheduled workflows may be disabled after 60 days of repo inactivity.
- No secrets are stored in the repo — only the ticker list.
