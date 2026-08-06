#!/usr/bin/env python3
"""Fetch daily US stock news from Finnhub and deliver via CallMeBot WhatsApp."""

import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

MAX_MESSAGE_LENGTH = 4000
HEADLINES_PER_TICKER = 3
FETCH_LIMIT_PER_TICKER = 10
SEND_DELAY_SECONDS = 2

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


def fetch_news(symbol: str, api_key: str, limit: int = FETCH_LIMIT_PER_TICKER) -> list[dict]:
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


def story_dedupe_key(item: dict) -> str:
    story_id = item.get("id")
    if story_id is not None:
        return str(story_id)
    headline = item.get("headline", "").strip().lower()
    if headline:
        return headline
    return item.get("url", "").strip().lower()


def format_relative_time(unix_ts: int | float | None) -> str:
    if not unix_ts:
        return ""
    published = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - published
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(1, minutes)}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return "Yesterday" if days == 1 else f"{days}d ago"


def format_story_meta(item: dict) -> str:
    source = item.get("source", "").strip() or "News"
    relative_time = format_relative_time(item.get("datetime"))
    if relative_time:
        return f"_{source} · {relative_time}_"
    return f"_{source}_"


def format_story_link(item: dict) -> str | None:
    url = item.get("url", "").strip()
    if not url:
        return None
    return f"   🔗 {url}"


def truncate_message(message: str) -> str:
    if len(message) <= MAX_MESSAGE_LENGTH:
        return message
    return message[: MAX_MESSAGE_LENGTH - 3].rstrip() + "..."


def format_ticker_section(
    ticker: str,
    news: list[dict],
    seen_stories: set[str],
) -> tuple[list[str], int]:
    lines: list[str] = [f"*{ticker}*"]
    story_count = 0
    story_number = 0

    for item in news:
        key = story_dedupe_key(item)
        if not key or key in seen_stories:
            continue

        seen_stories.add(key)
        story_number += 1
        story_count += 1

        headline = item.get("headline", "No headline").strip()
        lines.append(f"{story_number}. {headline}")
        lines.append(f"   {format_story_meta(item)}")
        link = format_story_link(item)
        if link:
            lines.append(link)

        if story_number >= HEADLINES_PER_TICKER:
            break

    if story_count == 0:
        lines = [f"_No major news for {ticker} today._"]

    return lines, story_count


def build_messages(tickers: list[str], api_key: str) -> list[str]:
    """Build one WhatsApp message per ticker, plus header and footer."""
    today_label = date.today().strftime("%d %b %Y")
    seen_stories: set[str] = set()
    total_stories = 0
    ticker_bodies: list[str] = []

    for ticker in tickers:
        try:
            news = fetch_news(ticker, api_key)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch news for {ticker}: {exc}", file=sys.stderr)
            ticker_bodies.append(f"*{ticker}*\n_Could not fetch news for {ticker}._")
            continue

        section_lines, story_count = format_ticker_section(ticker, news, seen_stories)
        total_stories += story_count
        ticker_bodies.append("\n".join(section_lines))

    total_parts = len(ticker_bodies) + 2  # header + tickers + footer
    messages: list[str] = []

    messages.append(
        truncate_message(
            f"📈 *Stock News — {today_label}*\n"
            f"_Part 1/{total_parts} · {len(tickers)} tickers · {total_stories} stories_"
        )
    )

    for index, body in enumerate(ticker_bodies, start=2):
        messages.append(
            truncate_message(f"_Part {index}/{total_parts}_\n\n{body}")
        )

    messages.append(
        truncate_message(
            f"_Part {total_parts}/{total_parts}_\n"
            f"──────────────\n"
            f"_{len(tickers)} tickers · {total_stories} stories · stock-news-bot_"
        )
    )

    return messages


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


def send_whatsapp_messages(messages: list[str], phone: str, api_key: str) -> None:
    total = len(messages)
    for index, message in enumerate(messages, start=1):
        print(f"Sending message {index}/{total} ({len(message)} chars)...")
        send_whatsapp(message, phone, api_key)
        if index < total:
            time.sleep(SEND_DELAY_SECONDS)


def main() -> None:
    finnhub_key = require_env("FINNHUB_API_KEY", FINNHUB_KEY)
    phone = require_env("WHATSAPP_PHONE", WHATSAPP_PHONE)
    callmebot_key = require_env("CALLMEBOT_API_KEY", CALLMEBOT_KEY)

    tickers = load_tickers()
    messages = build_messages(tickers, finnhub_key)
    total_chars = sum(len(message) for message in messages)
    print(f"Prepared {len(messages)} messages for {len(tickers)} tickers ({total_chars} chars total)")
    send_whatsapp_messages(messages, phone, callmebot_key)


if __name__ == "__main__":
    main()
