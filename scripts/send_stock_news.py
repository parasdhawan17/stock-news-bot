#!/usr/bin/env python3
"""Fetch daily US stock news from Finnhub and deliver via WhatsApp, email, and web."""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

MAX_MESSAGE_LENGTH = 4000
HEADLINES_PER_TICKER = 3
WEB_HEADLINES_PER_TICKER = FETCH_LIMIT_PER_TICKER = 10
SEND_DELAY_SECONDS = 2
SUMMARY_EXCERPT_LENGTH = 160
MAX_TICKERS_PER_USER = 10
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

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
BREVO_LIST_ID = os.environ.get("BREVO_LIST_ID")
BREVO_SUBSCRIBE_FORM_URL = os.environ.get("BREVO_SUBSCRIBE_FORM_URL", "").strip()
BREVO_TICKERS_ATTRIBUTE = os.environ.get("BREVO_TICKERS_ATTRIBUTE", "TICKERS").strip().upper()
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Stock News Bot")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = REPO_ROOT / "config" / "tickers.json"
TEMPLATES_PATH = REPO_ROOT / "templates"
DOCS_PATH = REPO_ROOT / "docs"
ARCHIVE_PATH = DOCS_PATH / "archive"


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


def parse_tickers(raw: str | list | tuple | None) -> list[str]:
    """Parse tickers from Brevo text or multiple-choice attribute values."""
    if raw is None:
        return []

    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw).strip()
        if not text:
            return []
        parts = re.split(r"[,;\s]+", text)

    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        ticker = part.strip().upper()
        if not ticker or ticker in seen:
            continue
        if not TICKER_PATTERN.match(ticker):
            continue
        seen.add(ticker)
        result.append(ticker)
        if len(result) >= MAX_TICKERS_PER_USER:
            break
    return result


def union_tickers(subscribers: list[dict]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for subscriber in subscribers:
        for ticker in subscriber.get("tickers", []):
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
    return result


def filter_sections(sections: list[dict], tickers: list[str]) -> list[dict]:
    ticker_set = set(tickers)
    return [section for section in sections if section["ticker"] in ticker_set]


def count_email_stories(sections: list[dict]) -> int:
    return sum(len(section.get("stories", [])) for section in sections)


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


def format_full_datetime(unix_ts: int | float | None) -> str:
    if not unix_ts:
        return ""
    published = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    hour = published.hour % 12 or 12
    ampm = "AM" if published.hour < 12 else "PM"
    return f"{published.day} {published.strftime('%b %Y')}, {hour}:{published.strftime('%M')} {ampm} UTC"


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
    *,
    excerpt: bool = True,
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
                "summary": excerpt_summary(summary) if excerpt and summary else summary,
                "image": sanitize_article_image(item.get("image")),
                "url": item.get("url", "").strip(),
                "source": item.get("source", "").strip() or "News",
                "relative_time": format_relative_time(item.get("datetime")),
                "published_at": format_full_datetime(item.get("datetime")),
            }
        )
        if len(stories) >= limit:
            break
    return stories


def select_web_stories(news: list[dict], limit: int = WEB_HEADLINES_PER_TICKER) -> list[dict]:
    return select_stories(news, set(), limit=limit, excerpt=False)


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
            "web_stories": [],
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
            section["web_stories"] = select_web_stories(news)
            section["hero_image"] = next((story["image"] for story in stories if story["image"]), None)
            total_stories += len(stories)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch news for {ticker}: {exc}", file=sys.stderr)
            section["error"] = str(exc)

        sections.append(section)

    return sections, total_stories


def format_whatsapp_quote(section: dict) -> str:
    ticker = section["ticker"]
    quote = section.get("quote")
    if not quote:
        return f"*{ticker}*"
    price = f"${quote['price']:.2f}"
    change_pct = quote.get("change_pct")
    if change_pct is None:
        return f"*{ticker}* · {price}"
    sign = "+" if change_pct >= 0 else ""
    indicator = "🟢" if change_pct >= 0 else "🔴"
    return f"*{ticker}* {indicator} {sign}{change_pct:.2f}% · {price}"


def format_hero_whatsapp(section: dict) -> str:
    lines = ["🔥 *TOP MOVER*", format_whatsapp_quote(section), "──────────────"]

    if section["error"]:
        lines.append(f"_Could not fetch news for {section['ticker']}._")
        return "\n".join(lines)

    if not section["stories"]:
        lines.append(f"_No major news for {section['ticker']} today._")
        return "\n".join(lines)

    for index, story in enumerate(section["stories"], start=1):
        lines.append(f"{index}. {story['headline']}")
        meta = story["source"]
        if story["relative_time"]:
            meta = f"{meta} · {story['relative_time']}"
        lines.append(f"   _{meta}_")
        if story["url"]:
            lines.append(f"   🔗 {story['url']}")

    return "\n".join(lines)


def format_compact_whatsapp(section: dict) -> str:
    lines = [format_whatsapp_quote(section)]

    if section["error"]:
        lines.append("_News unavailable_")
    elif not section["stories"]:
        lines.append("_No news today_")
    else:
        story = section["stories"][0]
        lines.append(f"• {story['headline']}")
        if story["url"]:
            lines.append(f"  🔗 {story['url']}")

    return "\n".join(lines)


def footer_text(ticker_count: int, story_count: int) -> str:
    line = f"{ticker_count} tickers · {story_count} stories · stock-news-bot"
    if SITE_URL:
        line += f"\nRead full digest: {SITE_URL}/"
    return line


def build_messages(sections: list[dict], tickers: list[str], total_stories: int) -> list[str]:
    """Build tiered WhatsApp messages: summary, top mover, compact movers, footer."""
    today_label = date.today().strftime("%d %b %Y")
    layout = prepare_email_layout(sections)
    summary = layout["market_summary"]
    compact_per_message = 3

    header_lines = [
        f"📈 *Stock News — {today_label}*",
        (
            f"_{len(tickers)} tickers · {total_stories} stories · "
            f"{summary['gainers']} up · {summary['losers']} down · {summary['flat']} flat_"
        ),
    ]
    if layout["top_mover_label"]:
        header_lines.append(f"_Top mover: {layout['top_mover_label']}_")

    if layout["movers_bar"]:
        mover_pills: list[str] = []
        for mover in layout["movers_bar"]:
            if mover["change_pct"] is not None:
                sign = "+" if mover["change_pct"] >= 0 else ""
                mover_pills.append(f"{mover['ticker']} {sign}{mover['change_pct']:.1f}%")
            else:
                mover_pills.append(mover["ticker"])
        header_lines.append(" · ".join(mover_pills))

    bodies: list[str] = ["\n".join(header_lines)]

    if layout["hero"]:
        bodies.append(format_hero_whatsapp(layout["hero"]))

    compact_sections = layout["compact"]
    for index in range(0, len(compact_sections), compact_per_message):
        batch = compact_sections[index : index + compact_per_message]
        compact_body = "📊 *Other Movers*\n──────────────\n\n" + "\n\n".join(
            format_compact_whatsapp(section) for section in batch
        )
        bodies.append(compact_body)

    bodies.append(
        f"──────────────\n_{footer_text(len(tickers), total_stories)}_"
    )

    total_parts = len(bodies)
    messages: list[str] = []
    for index, body in enumerate(bodies, start=1):
        prefix = f"_Part {index}/{total_parts}_\n\n" if total_parts > 1 else ""
        messages.append(truncate_message(f"{prefix}{body}"))

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
    env = get_jinja_env()
    template = env.get_template("email_digest.html")
    layout = prepare_email_layout(sections)
    html = template.render(
        date_label=today_label,
        ticker_count=len(tickers),
        story_count=total_stories,
        site_url=SITE_URL,
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

    lines.append(footer_text(ticker_count, story_count))
    return "\n".join(lines)


def get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_PATH),
        autoescape=select_autoescape(["html"]),
    )


def count_web_stories(sections: list[dict]) -> int:
    return sum(len(section.get("web_stories", [])) for section in sections)


def build_web_digest(
    sections: list[dict],
    tickers: list[str],
    *,
    is_archive: bool = False,
    archive_label: str | None = None,
) -> str:
    today_label = date.today().strftime("%d %b %Y")
    layout = prepare_email_layout(sections)
    web_story_count = count_web_stories(sections)
    archives = list_archives()
    archive_href_prefix = "" if is_archive else "archive/"
    env = get_jinja_env()
    template = env.get_template("web_digest.html")
    return template.render(
        date_label=today_label,
        ticker_count=len(tickers),
        story_count=web_story_count,
        site_url=SITE_URL,
        is_archive=is_archive,
        archive_label=archive_label,
        archives=archives,
        archive_href_prefix=archive_href_prefix,
        visible_story_count=HEADLINES_PER_TICKER,
        subscribe_form_url=BREVO_SUBSCRIBE_FORM_URL or None,
        **layout,
    )


def parse_archive_filename(path: Path) -> datetime | None:
    stem = path.stem
    try:
        return datetime.strptime(stem, "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_archives() -> list[dict]:
    if not ARCHIVE_PATH.is_dir():
        return []

    archives: list[dict] = []
    for path in ARCHIVE_PATH.glob("*.html"):
        if path.name == "index.html":
            continue
        generated_at = parse_archive_filename(path)
        if not generated_at:
            continue
        archives.append(
            {
                "filename": path.name,
                "label": generated_at.strftime("%d %b %Y, %H:%M UTC"),
                "generated_at": generated_at,
            }
        )

    archives.sort(key=lambda entry: entry["generated_at"], reverse=True)
    return archives


def build_archive_index(archives: list[dict]) -> str:
    env = get_jinja_env()
    template = env.get_template("archive_index.html")
    return template.render(archives=archives, site_url=SITE_URL)


def write_web_pages(sections: list[dict], tickers: list[str]) -> None:
    generated_at = datetime.now(timezone.utc)
    archive_slug = generated_at.strftime("%Y-%m-%d-%H%M")
    archive_label = generated_at.strftime("%d %b %Y, %H:%M UTC")

    html = build_web_digest(sections, tickers)
    archive_html = build_web_digest(
        sections,
        tickers,
        is_archive=True,
        archive_label=archive_label,
    )

    DOCS_PATH.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.mkdir(parents=True, exist_ok=True)
    (DOCS_PATH / ".nojekyll").touch(exist_ok=True)

    index_path = DOCS_PATH / "index.html"
    archive_path = ARCHIVE_PATH / f"{archive_slug}.html"

    index_path.write_text(html, encoding="utf-8")
    archive_path.write_text(archive_html, encoding="utf-8")

    archives = list_archives()
    archive_index_path = ARCHIVE_PATH / "index.html"
    archive_index_path.write_text(build_archive_index(archives), encoding="utf-8")

    print(f"Web digest written to {index_path}")
    print(f"Archive written to {archive_path}")
    print(f"Archive index updated ({len(archives)} entries)")


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


def fetch_subscribers_with_tickers(list_id: int, api_key: str) -> list[dict]:
    headers = {"api-key": api_key, "Accept": "application/json"}
    subscribers: list[dict] = []
    offset = 0
    limit = 50

    while True:
        response = requests.get(
            "https://api.brevo.com/v3/contacts",
            headers=headers,
            params={"limit": limit, "offset": offset, "listIds": [list_id]},
            timeout=30,
        )
        response.raise_for_status()
        contacts = response.json().get("contacts", [])
        if not contacts:
            break

        for contact in contacts:
            if contact.get("emailBlacklisted"):
                continue
            email = contact.get("email", "").strip()
            if not email:
                continue
            attributes = contact.get("attributes") or {}
            raw_tickers = attributes.get(BREVO_TICKERS_ATTRIBUTE, "")
            tickers = parse_tickers(raw_tickers)
            subscribers.append({"email": email, "tickers": tickers})

        if len(contacts) < limit:
            break
        offset += limit

    return subscribers


def send_email(
    html: str,
    text: str,
    api_key: str,
    sender_email: str,
    recipients: list[str],
    sender_name: str,
) -> None:
    today_label = date.today().strftime("%d %b %Y")
    subject = f"Stock News — {today_label}"
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload_base = {
        "sender": {"name": sender_name, "email": sender_email},
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }

    total = len(recipients)
    for index, recipient_email in enumerate(recipients, start=1):
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers=headers,
            json={**payload_base, "to": [{"email": recipient_email}]},
            timeout=30,
        )
        response.raise_for_status()
        message_id = response.json().get("messageId", response.text)
        print(f"Email sent to {recipient_email} ({index}/{total}): {message_id}")
        if index < total:
            time.sleep(SEND_DELAY_SECONDS)


def resolve_digest_tickers(subscribers: list[dict]) -> list[str]:
    union = union_tickers(subscribers)
    if union:
        print(f"Using {len(union)} ticker(s) from subscriber union")
        return union
    print("No subscriber tickers found; using config/tickers.json")
    return load_tickers()


def email_configured() -> bool:
    has_recipients = bool(BREVO_LIST_ID or EMAIL_TO)
    return bool(BREVO_API_KEY and EMAIL_FROM and has_recipients)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch stock news and publish the web digest. Delivery channels are opt-in.",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send the HTML email digest via Brevo.",
    )
    parser.add_argument(
        "--whatsapp",
        action="store_true",
        help="Send the WhatsApp digest via CallMeBot.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Send via every configured delivery channel (email and WhatsApp).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    send_email_digest = args.email or args.all
    send_whatsapp_digest = args.whatsapp or args.all

    finnhub_key = require_env("FINNHUB_API_KEY", FINNHUB_KEY)

    subscribers: list[dict] = []
    if BREVO_LIST_ID and BREVO_API_KEY:
        subscribers = fetch_subscribers_with_tickers(int(BREVO_LIST_ID), BREVO_API_KEY)
        print(f"Fetched {len(subscribers)} subscriber(s) from Brevo list {BREVO_LIST_ID}")
        digest_tickers = resolve_digest_tickers(subscribers)
    else:
        digest_tickers = load_tickers()

    sections, _ = collect_digest_data(digest_tickers, finnhub_key)
    write_web_pages(sections, digest_tickers)

    if send_whatsapp_digest:
        phone = require_env("WHATSAPP_PHONE", WHATSAPP_PHONE)
        callmebot_key = require_env("CALLMEBOT_API_KEY", CALLMEBOT_KEY)
        whatsapp_tickers = load_tickers()
        if whatsapp_tickers == digest_tickers:
            whatsapp_sections = sections
            whatsapp_total_stories = count_email_stories(sections)
        else:
            whatsapp_sections, whatsapp_total_stories = collect_digest_data(
                whatsapp_tickers, finnhub_key
            )
        messages = build_messages(whatsapp_sections, whatsapp_tickers, whatsapp_total_stories)
        total_chars = sum(len(message) for message in messages)
        print(
            f"Prepared {len(messages)} WhatsApp messages for {len(whatsapp_tickers)} tickers "
            f"({total_chars} chars total)"
        )
        send_whatsapp_messages(messages, phone, callmebot_key)
    else:
        print("WhatsApp skipped (pass --whatsapp to enable).")

    if send_email_digest:
        if not email_configured():
            print(
                "Error: --email requested but BREVO_API_KEY, EMAIL_FROM, and "
                "BREVO_LIST_ID or EMAIL_TO are not set.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            if BREVO_LIST_ID:
                if not subscribers and BREVO_API_KEY:
                    subscribers = fetch_subscribers_with_tickers(
                        int(BREVO_LIST_ID), BREVO_API_KEY
                    )
                if not subscribers:
                    print("No email recipients found; skipping email send.")
                else:
                    sent_count = 0
                    for subscriber in subscribers:
                        email = subscriber["email"]
                        user_tickers = subscriber["tickers"]
                        if not user_tickers:
                            print(f"Skipped {email}: no valid tickers")
                            continue
                        user_sections = filter_sections(sections, user_tickers)
                        user_story_count = count_email_stories(user_sections)
                        html, text = build_email_digest(
                            user_sections, user_tickers, user_story_count
                        )
                        print(
                            f"Prepared email for {email} "
                            f"({len(user_tickers)} tickers, {len(html)} chars HTML)"
                        )
                        send_email(
                            html,
                            text,
                            BREVO_API_KEY,
                            EMAIL_FROM,
                            [email],
                            EMAIL_FROM_NAME,
                        )
                        sent_count += 1
                    if sent_count:
                        print(f"Sent {sent_count} personalized email(s)")
            elif EMAIL_TO:
                fallback_tickers = load_tickers()
                email_sections = (
                    filter_sections(sections, fallback_tickers)
                    if digest_tickers != fallback_tickers
                    else sections
                )
                email_story_count = count_email_stories(email_sections)
                html, text = build_email_digest(
                    email_sections, fallback_tickers, email_story_count
                )
                print(
                    f"Prepared email for {EMAIL_TO} "
                    f"({len(html)} chars HTML, {len(text)} chars plain text)"
                )
                send_email(
                    html, text, BREVO_API_KEY, EMAIL_FROM, [EMAIL_TO], EMAIL_FROM_NAME
                )
            else:
                print("No email recipients found; skipping email send.")
        except requests.RequestException as exc:
            print(f"Warning: failed to send email: {exc}", file=sys.stderr)
    else:
        print("Email skipped (pass --email to enable).")


if __name__ == "__main__":
    main()
