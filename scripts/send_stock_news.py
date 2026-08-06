#!/usr/bin/env python3
"""Fetch daily US stock news from Finnhub and deliver via CallMeBot WhatsApp."""

import json
import os
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import requests

MAX_MESSAGE_LENGTH = 4000
HEADLINES_PER_TICKER = 3

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE")
CALLMEBOT_KEY = os.environ.get("CALLMEBOT_API_KEY")

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = REPO_ROOT / "config" / "tickers.json"


def require_env(name: str, value: str | None) -> str:
    if not value:
        print(f"Error: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return value


def load_tickers() -> list[str]:
    data = json.loads(TICKERS_PATH.read_text(encoding="utf-8"))
    tickers = data.get("tickers", [])
    if not tickers:
        print("Error: no tickers configured in config/tickers.json", file=sys.stderr)
        sys.exit(1)
    return tickers


def fetch_news(symbol: str, api_key: str, limit: int = HEADLINES_PER_TICKER) -> list[dict]:
    today = date.today()
    yesterday = today - timedelta(days=1)

    response = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": symbol,
            "from": yesterday.isoformat(),
            "to": today.isoformat(),
            "token": api_key,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()[:limit]


def build_message(tickers: list[str], api_key: str) -> str:
    lines = [f"Daily Stock News — {date.today().strftime('%d %b %Y')}", ""]

    for ticker in tickers:
        lines.append(ticker)
        try:
            news = fetch_news(ticker, api_key)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch news for {ticker}: {exc}", file=sys.stderr)
            lines.append("• Could not fetch news")
            lines.append("")
            continue

        if not news:
            lines.append("• No major news in the last 24h")
        else:
            for item in news:
                headline = item.get("headline", "No headline")
                url = item.get("url", "")
                lines.append(f"• {headline}")
                if url:
                    lines.append(f"  {url}")
        lines.append("")

    message = "\n".join(lines).strip()
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 3].rstrip() + "..."
    return message


def send_whatsapp(message: str, phone: str, api_key: str) -> None:
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={urllib.parse.quote(phone)}"
        f"&text={urllib.parse.quote(message)}"
        f"&apikey={urllib.parse.quote(api_key)}"
    )
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    print("WhatsApp message sent:", response.text)


def main() -> None:
    finnhub_key = require_env("FINNHUB_API_KEY", FINNHUB_KEY)
    phone = require_env("WHATSAPP_PHONE", WHATSAPP_PHONE)
    callmebot_key = require_env("CALLMEBOT_API_KEY", CALLMEBOT_KEY)

    tickers = load_tickers()
    message = build_message(tickers, finnhub_key)
    print(f"Prepared message for {len(tickers)} tickers ({len(message)} chars)")
    send_whatsapp(message, phone, callmebot_key)


if __name__ == "__main__":
    main()
