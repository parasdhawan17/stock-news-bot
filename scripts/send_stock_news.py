#!/usr/bin/env python3
"""Fetch daily US stock news from Finnhub and deliver via WhatsApp and email."""

import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

MAX_MESSAGE_LENGTH = 4000
HEADLINES_PER_TICKER = 3
FETCH_LIMIT_PER_TICKER = 10
SEND_DELAY_SECONDS = 2
SUMMARY_EXCERPT_LENGTH = 160

# Finnhub often returns publisher branding instead of article photos.
PUBLISHER_LOGO_MARKERS = (
    "yahoo_finance",
    "/rz/stage/p/",
    "yimg.com/rz/",
    "seekingalpha.com/assets/images/sa_logo",
    "benzinga.com/sites/all/themes/benzinga",
    "foolcdn.com/media/affiliates/logos",
)

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE")
CALLMEBOT_KEY = os.environ.get("CALLMEBOT_API_KEY")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Stock News Bot")

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = REPO_ROOT / "config" / "tickers.json"
TEMPLATES_PATH = REPO_ROOT / "templates"


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


def fetch_quote(symbol: str, api_key: str) -> dict | None:
    response = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": api_key},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data or data.get("c") in (None, 0):
        return None
    return {"price": data["c"], "change_pct": data.get("dp")}


def fetch_company_logo(symbol: str, api_key: str) -> str | None:
    response = requests.get(
        "https://finnhub.io/api/v1/stock/profile2",
        params={"symbol": symbol, "token": api_key},
        timeout=30,
    )
    response.raise_for_status()
    logo = response.json().get("logo", "").strip()
    return logo or None


def is_usable_article_image(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    return not any(marker in lower for marker in PUBLISHER_LOGO_MARKERS)


def sanitize_article_image(url: str | None) -> str | None:
    cleaned = (url or "").strip()
    return cleaned if is_usable_article_image(cleaned) else None


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


def excerpt_summary(text: str, max_length: int = SUMMARY_EXCERPT_LENGTH) -> str:
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


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


def select_stories(
    news: list[dict],
    seen_stories: set[str],
    limit: int = HEADLINES_PER_TICKER,
) -> list[dict]:
    stories: list[dict] = []
    for item in news:
        key = story_dedupe_key(item)
        if not key or key in seen_stories:
            continue

        seen_stories.add(key)
        summary = item.get("summary", "").strip()
        stories.append(
            {
                "headline": item.get("headline", "No headline").strip(),
                "summary": excerpt_summary(summary) if summary else "",
                "image": sanitize_article_image(item.get("image")),
                "url": item.get("url", "").strip(),
                "source": item.get("source", "").strip() or "News",
                "relative_time": format_relative_time(item.get("datetime")),
            }
        )
        if len(stories) >= limit:
            break
    return stories


def collect_digest_data(tickers: list[str], api_key: str) -> tuple[list[dict], int]:
    seen_stories: set[str] = set()
    sections: list[dict] = []
    total_stories = 0

    for ticker in tickers:
        section: dict = {
            "ticker": ticker,
            "quote": None,
            "logo": None,
            "hero_image": None,
            "stories": [],
            "error": None,
        }

        try:
            section["quote"] = fetch_quote(ticker, api_key)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch quote for {ticker}: {exc}", file=sys.stderr)

        try:
            section["logo"] = fetch_company_logo(ticker, api_key)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch logo for {ticker}: {exc}", file=sys.stderr)

        try:
            news = fetch_news(ticker, api_key)
            stories = select_stories(news, seen_stories)
            section["stories"] = stories
            section["hero_image"] = next((story["image"] for story in stories if story["image"]), None)
            total_stories += len(stories)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch news for {ticker}: {exc}", file=sys.stderr)
            section["error"] = str(exc)

        sections.append(section)

    return sections, total_stories


def format_ticker_section_whatsapp(ticker: str, stories: list[dict], error: str | None) -> list[str]:
    if error:
        return [f"*{ticker}*\n_Could not fetch news for {ticker}._"]

    if not stories:
        return [f"_No major news for {ticker} today._"]

    lines: list[str] = [f"*{ticker}*"]
    for index, story in enumerate(stories, start=1):
        lines.append(f"{index}. {story['headline']}")
        meta = story["source"]
        if story["relative_time"]:
            meta = f"{meta} · {story['relative_time']}"
        lines.append(f"   _{meta}_")
        if story["url"]:
            lines.append(f"   🔗 {story['url']}")
    return lines


def build_messages(sections: list[dict], tickers: list[str], total_stories: int) -> list[str]:
    """Build one WhatsApp message per ticker, plus header and footer."""
    today_label = date.today().strftime("%d %b %Y")
    ticker_bodies: list[str] = []

    for section in sections:
        lines = format_ticker_section_whatsapp(
            section["ticker"], section["stories"], section["error"]
        )
        ticker_bodies.append("\n".join(lines))

    total_parts = len(ticker_bodies) + 2
    messages: list[str] = []

    messages.append(
        truncate_message(
            f"📈 *Stock News — {today_label}*\n"
            f"_Part 1/{total_parts} · {len(tickers)} tickers · {total_stories} stories_"
        )
    )

    for index, body in enumerate(ticker_bodies, start=2):
        messages.append(truncate_message(f"_Part {index}/{total_parts}_\n\n{body}"))

    messages.append(
        truncate_message(
            f"_Part {total_parts}/{total_parts}_\n"
            f"──────────────\n"
            f"_{len(tickers)} tickers · {total_stories} stories · stock-news-bot_"
        )
    )

    return messages


def abs_change_pct(section: dict) -> float:
    quote = section.get("quote")
    if not quote or quote.get("change_pct") is None:
        return 0.0
    return abs(quote["change_pct"])


def format_mover_label(ticker: str, change_pct: float | None) -> str:
    if change_pct is None:
        return ticker
    sign = "+" if change_pct >= 0 else ""
    return f"{ticker} {sign}{change_pct:.2f}%"


def prepare_email_layout(sections: list[dict]) -> dict:
    has_quotes = any(
        s.get("quote") and s["quote"].get("change_pct") is not None for s in sections
    )

    ranked = sorted(sections, key=abs_change_pct, reverse=True)

    if len(sections) == 1:
        hero = sections[0]
        compact = []
    elif has_quotes:
        hero = ranked[0]
        compact = ranked[1:]
    else:
        hero = None
        compact = list(sections)

    movers_bar: list[dict] = []
    gainers = losers = flat = 0

    for section in sections:
        quote = section.get("quote")
        change_pct = quote.get("change_pct") if quote else None
        price = quote.get("price") if quote else None

        if change_pct is None:
            flat += 1
            is_positive = None
        elif change_pct > 0:
            gainers += 1
            is_positive = True
        elif change_pct < 0:
            losers += 1
            is_positive = False
        else:
            flat += 1
            is_positive = None

        movers_bar.append(
            {
                "ticker": section["ticker"],
                "price": price,
                "change_pct": change_pct,
                "is_positive": is_positive,
            }
        )

    movers_bar.sort(key=lambda m: abs(m["change_pct"] or 0), reverse=True)

    top_mover_label = None
    if hero:
        hero_change = hero.get("quote", {}).get("change_pct") if hero.get("quote") else None
        top_mover_label = format_mover_label(hero["ticker"], hero_change)

    return {
        "hero": hero,
        "compact": compact,
        "movers_bar": movers_bar,
        "market_summary": {"gainers": gainers, "losers": losers, "flat": flat},
        "top_mover_label": top_mover_label,
    }


def build_email_digest(
    sections: list[dict], tickers: list[str], total_stories: int
) -> tuple[str, str]:
    today_label = date.today().strftime("%d %b %Y")
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_PATH),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("email_digest.html")
    layout = prepare_email_layout(sections)
    html = template.render(
        date_label=today_label,
        ticker_count=len(tickers),
        story_count=total_stories,
        **layout,
    )
    text = build_plain_text(layout, today_label, len(tickers), total_stories)
    return html, text


def format_section_plain_text(section: dict, *, compact: bool = False) -> list[str]:
    lines: list[str] = []
    ticker_line = section["ticker"]
    if section["quote"]:
        quote = section["quote"]
        ticker_line += f"  ${quote['price']:.2f}"
        if quote["change_pct"] is not None:
            sign = "+" if quote["change_pct"] >= 0 else ""
            ticker_line += f"  {sign}{quote['change_pct']:.2f}%"
    lines.append(ticker_line)
    lines.append("-" * len(ticker_line))

    if section["error"]:
        lines.append(f"Could not fetch news for {section['ticker']}.")
    elif not section["stories"]:
        lines.append(f"No major news for {section['ticker']} today.")
    elif compact:
        story = section["stories"][0]
        lines.append(f"• {story['headline']}")
        if story["url"]:
            lines.append(f"  {story['url']}")
    else:
        for index, story in enumerate(section["stories"], start=1):
            lines.append(f"{index}. {story['headline']}")
            if story["summary"]:
                lines.append(f"   {story['summary']}")
            meta = story["source"]
            if story["relative_time"]:
                meta = f"{meta} · {story['relative_time']}"
            lines.append(f"   {meta}")
            if story["url"]:
                lines.append(f"   {story['url']}")
            lines.append("")

    lines.append("")
    return lines


def build_plain_text(
    layout: dict, date_label: str, ticker_count: int, story_count: int
) -> str:
    summary = layout["market_summary"]
    lines = [
        f"Stock News — {date_label}",
        f"{ticker_count} tickers · {story_count} stories",
        f"{summary['gainers']} up · {summary['losers']} down · {summary['flat']} flat",
        "",
    ]

    if layout["top_mover_label"]:
        lines.append(f"Top mover today: {layout['top_mover_label']}")
        lines.append("")

    if layout["hero"]:
        lines.append("=== TOP MOVER ===")
        lines.extend(format_section_plain_text(layout["hero"], compact=False))

    for section in layout["compact"]:
        lines.extend(format_section_plain_text(section, compact=True))

    lines.append(f"{ticker_count} tickers · {story_count} stories · stock-news-bot")
    return "\n".join(lines)


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


def send_email(
    html: str,
    text: str,
    api_key: str,
    sender_email: str,
    recipient_email: str,
    sender_name: str,
) -> None:
    today_label = date.today().strftime("%d %b %Y")
    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": recipient_email}],
            "subject": f"Stock News — {today_label}",
            "htmlContent": html,
            "textContent": text,
        },
        timeout=30,
    )
    response.raise_for_status()
    print("Email sent:", response.json().get("messageId", response.text))


def email_configured() -> bool:
    return bool(BREVO_API_KEY and EMAIL_TO and EMAIL_FROM)


def main() -> None:
    finnhub_key = require_env("FINNHUB_API_KEY", FINNHUB_KEY)
    phone = require_env("WHATSAPP_PHONE", WHATSAPP_PHONE)
    callmebot_key = require_env("CALLMEBOT_API_KEY", CALLMEBOT_KEY)

    tickers = load_tickers()
    sections, total_stories = collect_digest_data(tickers, finnhub_key)

    messages = build_messages(sections, tickers, total_stories)
    total_chars = sum(len(message) for message in messages)
    print(f"Prepared {len(messages)} messages for {len(tickers)} tickers ({total_chars} chars total)")
    send_whatsapp_messages(messages, phone, callmebot_key)

    if not email_configured():
        print("Email skipped: set BREVO_API_KEY, EMAIL_TO, and EMAIL_FROM to enable.")
        return

    try:
        html, text = build_email_digest(sections, tickers, total_stories)
        print(f"Prepared email ({len(html)} chars HTML, {len(text)} chars plain text)")
        send_email(html, text, BREVO_API_KEY, EMAIL_FROM, EMAIL_TO, EMAIL_FROM_NAME)
    except requests.RequestException as exc:
        print(f"Warning: failed to send email: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
