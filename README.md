# Stock News Bot

Daily US stock news digest delivered to WhatsApp, email, and a GitHub Pages web digest.

**Cost: $0** — Finnhub free API, CallMeBot free WhatsApp delivery, Brevo free email tier, GitHub Pages, GitHub Actions on a public repo.

## How it works

1. GitHub Actions runs twice daily (9:00 AM and 8:30 PM IST) or manually via **Run workflow**.
2. The script reads tickers from `config/tickers.json`.
3. Finnhub fetches the last 24 hours of headlines and real-time quotes per ticker.
4. A formatted message is sent to your WhatsApp via CallMeBot.
5. A rich HTML email digest (with thumbnails, summaries, and price changes) is sent via Brevo.
6. A detailed web digest is generated into `docs/` and published via GitHub Pages (up to 10 full stories per ticker).

## Project structure

```
stock-news-bot/
├── .github/workflows/daily-stock-news.yml
├── config/tickers.json
├── docs/                          # Generated web digest (GitHub Pages)
├── scripts/send_stock_news.py
├── templates/
│   ├── email_digest.html
│   ├── web_digest.html
│   └── archive_index.html
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

### 3. Brevo email setup

1. Sign up at [brevo.com](https://www.brevo.com/) (free tier: 300 emails/day).
2. Go to **Settings → Senders** and verify your sender email address.
3. Go to **SMTP & API → API Keys** and create a key with transactional send permission.

### 4. GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|--------|-------|
| `FINNHUB_API_KEY` | Your Finnhub API key |
| `WHATSAPP_PHONE` | Your number with country code, e.g. `+919876543210` |
| `CALLMEBOT_API_KEY` | API key from CallMeBot |
| `BREVO_API_KEY` | API key from Brevo |
| `EMAIL_TO` | Recipient email, e.g. `you@gmail.com` |
| `EMAIL_FROM` | Verified sender email in Brevo |
| `EMAIL_FROM_NAME` | Display name, e.g. `Stock News Bot` (optional) |

### 5. Push to GitHub and configure secrets

Use a **public** repo for unlimited free GitHub Actions minutes.

**Option A — automated (recommended):**

```bash
cd /Users/anisharajput/Documents/Github/stock-news-bot
export FINNHUB_API_KEY="your-finnhub-key"
export WHATSAPP_PHONE="+91xxxxxxxxxx"
export CALLMEBOT_API_KEY="your-callmebot-key"
export BREVO_API_KEY="your-brevo-api-key"
export EMAIL_TO="you@example.com"
export EMAIL_FROM="digest@yourdomain.com"
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

### 6. Enable GitHub Pages

1. In your repo: **Settings → Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Set **Branch** to `main` and **Folder** to `/docs`.
4. Save. The site will be live at `https://<your-username>.github.io/<repo-name>/`.
5. Past digests are archived at `/archive/` (each workflow run creates a new dated page).

The workflow sets `SITE_URL` automatically so WhatsApp and email footers link to the web digest.

### 7. Test

1. Open **Actions** → **Daily Stock News** → **Run workflow**.
2. Check the run logs for `Web digest written`, `WhatsApp message sent`, and `Email sent`.
3. Confirm the WhatsApp message, HTML email, and web page on your devices.

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

This generates `docs/index.html` locally. Open it in a browser to preview the web digest.

Email is skipped if `BREVO_API_KEY`, `EMAIL_TO`, or `EMAIL_FROM` are not set.

Set `SITE_URL` in `.env` to include the web digest link in WhatsApp and email footers during local runs.

## Schedule

The workflow runs at:
- `30 3 * * *` UTC = **9:00 AM IST**
- `0 15 * * *` UTC = **8:30 PM IST**

GitHub cron can run a few minutes late on the free tier — fine for a morning/evening digest.

## Notes

- CallMeBot is unofficial and for personal use only.
- Brevo requires a verified sender address for reliable delivery.
- Scheduled workflows may be disabled after 60 days of repo inactivity.
- No secrets are stored in the repo — only the ticker list.
