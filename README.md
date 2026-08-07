# Stock News Bot

A zero-cost daily US stock news digest. Fetches headlines and quotes from Finnhub, publishes a live web digest on GitHub Pages, and optionally delivers via email or WhatsApp.

**Cost: $0** — Finnhub free API, Brevo free email tier, GitHub Pages, GitHub Actions on a public repo. WhatsApp via CallMeBot is also free if enabled.

## Features

- **Web digest** — published to GitHub Pages with top movers highlighted, mover-ranked sections, 3 stories per ticker (expand for up to 10), and a collapsible in-page archive browser
- **Email digest** — rich HTML email with tiered mover layout, thumbnails, and summaries via Brevo
- **Email subscriptions** — Brevo embedded subscribe form on the web digest; workflow sends to all list contacts
- **WhatsApp digest** — tiered plain-text messages via CallMeBot (optional, disabled by default in CI)
- **Automated** — runs twice daily on GitHub Actions; each run archives a timestamped snapshot

## How it works

1. GitHub Actions runs twice daily (9:00 AM and 8:30 PM IST) or manually via **Run workflow**.
2. The script reads tickers from `config/tickers.json`.
3. Finnhub fetches the last 24 hours of headlines and real-time quotes per ticker.
4. A web digest is always generated into `docs/` and published to GitHub Pages.
5. Email is sent when `--email` is passed (enabled in the GitHub workflow).
6. WhatsApp is sent when `--whatsapp` is passed (opt-in; not enabled in CI by default).

## Delivery channels

The web digest always runs. Email and WhatsApp are opt-in via CLI flags:

| Command | Web | Email | WhatsApp |
|---------|-----|-------|----------|
| `python scripts/send_stock_news.py` | yes | — | — |
| `python scripts/send_stock_news.py --email` | yes | yes | — |
| `python scripts/send_stock_news.py --whatsapp` | yes | — | yes |
| `python scripts/send_stock_news.py --all` | yes | yes | yes |

Credentials are only required for the channels you enable. The GitHub workflow runs with `--email` only.

To re-enable WhatsApp in CI, add the WhatsApp secrets back and update the workflow command:

```yaml
run: python scripts/send_stock_news.py --email --whatsapp
# or
run: python scripts/send_stock_news.py --all
```

## Web digest

After enabling GitHub Pages, the site is live at:

```
https://<your-username>.github.io/<repo-name>/
```

Each workflow run:

- Overwrites `docs/index.html` with the latest digest
- Saves a snapshot to `docs/archive/YYYY-MM-DD-HHMM.html`
- Updates the archive index at `docs/archive/index.html`

The digest UI includes a movers summary bar, a featured top-mover section, ranked ticker cards, a collapsible "Browse past digests" panel, and an optional email subscribe form.

## Email subscriptions

Visitors can subscribe from the web digest via a Brevo embedded form. Each workflow run fetches all contacts from your Brevo list and sends the digest to each subscriber individually.

### One-time Brevo setup

1. In Brevo: **Contacts → Lists** → create a list (e.g. "Stock News Subscribers") and note the **list ID** (shown in the list URL or settings).
2. In Brevo: **Forms** → create a subscription form linked to that list.
   - Enable **double opt-in** (recommended) so only confirmed addresses receive mail.
3. Open the form's **Share** settings and copy the **embed URL** (the iframe `src`, e.g. `https://my.brevo.com/subscribe/...`).

### Limits

- **Brevo free tier:** 300 emails/day. With 2 runs/day, you can support roughly 150 subscribers.
- **Double opt-in:** New signups must confirm via Brevo before they appear in the list and receive digests.
- **Unsubscribe:** Managed automatically by Brevo for list contacts.

## Project structure

```
stock-news-bot/
├── .github/workflows/daily-stock-news.yml
├── config/tickers.json              # Ticker watchlist
├── docs/                            # Generated web digest (GitHub Pages)
│   ├── index.html
│   └── archive/
├── scripts/
│   ├── send_stock_news.py           # Main script
│   ├── bootstrap.sh                 # One-time GitHub setup (optional)
│   └── configure_secrets.sh         # Set GitHub Actions secrets (optional)
├── templates/
│   ├── web_digest.html
│   ├── email_digest.html
│   └── archive_index.html
├── .env.example
├── requirements.txt
└── README.md
```

## Setup

### 1. Finnhub API key (required)

1. Sign up at [finnhub.io](https://finnhub.io/).
2. Copy your API key from the dashboard.

### 2. Brevo email setup (required for `--email`)

1. Sign up at [brevo.com](https://www.brevo.com/) (free tier: 300 emails/day).
2. Go to **Settings → Senders** and verify your sender email address.
3. Go to **SMTP & API → API Keys** and create a key with transactional send permission.
4. Set up a contact list and subscription form (see [Email subscriptions](#email-subscriptions)).

### 3. CallMeBot WhatsApp setup (optional, for `--whatsapp`)

1. Save **+34 644 44 71 67** as a WhatsApp contact (e.g. "CallMeBot").
2. Send it: `I allow callmebot to send me messages`
3. It replies with your personal API key — save this for later.

### 4. GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

**Required for the default workflow:**

| Secret | Value |
|--------|-------|
| `FINNHUB_API_KEY` | Your Finnhub API key |
| `BREVO_API_KEY` | API key from Brevo |
| `BREVO_LIST_ID` | Numeric ID of your Brevo subscriber list |
| `BREVO_SUBSCRIBE_FORM_URL` | Full iframe embed URL from your Brevo form |
| `EMAIL_FROM` | Verified sender email in Brevo |
| `EMAIL_FROM_NAME` | Display name, e.g. `Stock News Bot` (optional) |

**Optional fallback (single recipient instead of a list):**

| Secret | Value |
|--------|-------|
| `EMAIL_TO` | Recipient email, e.g. `you@gmail.com` (used only when `BREVO_LIST_ID` is not set) |

**Only if using `--whatsapp`:**

| Secret | Value |
|--------|-------|
| `WHATSAPP_PHONE` | Your number with country code, e.g. `+919876543210` |
| `CALLMEBOT_API_KEY` | API key from CallMeBot |

`SITE_URL` is set automatically in the workflow so email footers link to the web digest.

### 5. Push to GitHub

Use a **public** repo for unlimited free GitHub Actions minutes.

**Option A — GitHub CLI:**

```bash
export FINNHUB_API_KEY="your-finnhub-key"
export BREVO_API_KEY="your-brevo-api-key"
export BREVO_LIST_ID="2"
export BREVO_SUBSCRIBE_FORM_URL="https://my.brevo.com/subscribe/your-form-id"
export EMAIL_FROM="digest@yourdomain.com"
# For email-only, add secrets via GitHub UI (see Option B).
# configure_secrets.sh also requires WhatsApp vars if you use it:
export WHATSAPP_PHONE="+91xxxxxxxxxx"
export CALLMEBOT_API_KEY="your-callmebot-key"
./scripts/configure_secrets.sh
git push -u origin main
```

**Option B — manual (recommended for email-only):**

1. Push the repo to GitHub.
2. Add the required secrets in **Settings → Secrets and variables → Actions**.

### 6. Enable GitHub Pages

1. In your repo: **Settings → Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Set **Branch** to `main` and **Folder** to `/docs`.
4. Save.

### 7. Test

1. Open **Actions → Daily Stock News → Run workflow**.
2. Check the run logs for `Web digest written` and `Email sent to ...`.
3. Open the web digest URL, confirm the subscribe form appears, and verify email delivery.

## Customize tickers

Edit `config/tickers.json`:

```json
{
  "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]
}
```

US symbols use plain tickers. Keep the list to ~10 tickers to stay within WhatsApp message limits if you enable that channel.

## Local testing

Copy `.env.example` to `.env` and fill in your keys:

```bash
pip install -r requirements.txt
set -a && source .env && set +a

python scripts/send_stock_news.py              # web digest only
python scripts/send_stock_news.py --email      # web + email
python scripts/send_stock_news.py --whatsapp   # web + WhatsApp
python scripts/send_stock_news.py --all        # web + email + WhatsApp
```

This writes `docs/index.html` locally. Open it in a browser to preview the digest.

Set `SITE_URL` in `.env` to include the web digest link in email and WhatsApp footers during local runs.

## Schedule

The workflow runs at:

| Cron (UTC) | Time (IST) |
|------------|------------|
| `30 3 * * *` | 9:00 AM |
| `0 15 * * *` | 8:30 PM |

GitHub cron can run a few minutes late on the free tier — fine for a morning/evening digest.

## Notes

- CallMeBot is unofficial and for personal use only.
- Brevo requires a verified sender address for reliable delivery.
- Scheduled workflows may be disabled after 60 days of repo inactivity.
- No secrets are stored in the repo — only the ticker list and generated `docs/` output.
