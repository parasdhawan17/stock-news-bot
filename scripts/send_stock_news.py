#!/usr/bin/env python3
"""Fetch daily US stock news from Finnhub and deliver via WhatsApp, email, and web."""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone, time as time_of_day
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

MAX_MESSAGE_LENGTH = 4000
HEADLINES_PER_TICKER = 3
DIGEST_HEADING = "Your stock news briefing"
WEB_HEADLINES_PER_TICKER = FETCH_LIMIT_PER_TICKER = 10
SEND_DELAY_SECONDS = 2
SUMMARY_EXCERPT_LENGTH = 160
MAX_TICKERS_PER_USER = 10
MAX_SUBJECT_MOVERS = 3
MAX_SUBJECT_HEADLINES = 3
SUBJECT_MAX_LEN = 78
HEADLINE_SNIPPET_LEN = 32
MIN_RELEVANCE_SCORE = 3
MIN_STORIES_PER_TICKER = 2
HEADLINE_ALIAS_POINTS = 3
SUMMARY_ALIAS_POINTS = 1
TICKER_SYMBOL_BONUS = 1
RIVAL_PENALTY = 3
ET_ZONE = ZoneInfo("America/New_York")
MARKET_OPEN_ET = time_of_day(9, 30)
MARKET_CLOSE_ET = time_of_day(16, 0)
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
TICKER_ALIASES_PATH = REPO_ROOT / "config" / "ticker_aliases.json"
TEMPLATES_PATH = REPO_ROOT / "templates"
DOCS_PATH = REPO_ROOT / "docs"
ARCHIVE_PATH = DOCS_PATH / "archive"

_TICKER_ALIASES_CACHE: dict[str, list[str]] | None = None
_ALIAS_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


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


def load_ticker_aliases() -> dict[str, list[str]]:
    """Load alias map from config; keys are uppercased ticker symbols."""
    global _TICKER_ALIASES_CACHE
    if _TICKER_ALIASES_CACHE is not None:
        return _TICKER_ALIASES_CACHE

    if not TICKER_ALIASES_PATH.is_file():
        _TICKER_ALIASES_CACHE = {}
        return _TICKER_ALIASES_CACHE

    data = json.loads(TICKER_ALIASES_PATH.read_text(encoding="utf-8"))
    raw = data.get("aliases", data)
    aliases: dict[str, list[str]] = {}
    for ticker, values in raw.items():
        key = str(ticker).strip().upper()
        if not key:
            continue
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values or []:
            alias = str(value).strip()
            if not alias:
                continue
            lowered = alias.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            cleaned.append(alias)
        if cleaned:
            aliases[key] = cleaned
    _TICKER_ALIASES_CACHE = aliases
    return _TICKER_ALIASES_CACHE


def aliases_for(ticker: str) -> list[str]:
    ticker = ticker.strip().upper()
    aliases = load_ticker_aliases().get(ticker, [])
    if ticker and ticker.lower() not in {a.lower() for a in aliases}:
        return [ticker, *aliases]
    return aliases or ([ticker] if ticker else [])


def _alias_pattern(alias: str) -> re.Pattern[str]:
    cached = _ALIAS_PATTERN_CACHE.get(alias)
    if cached is not None:
        return cached
    escaped = re.escape(alias)
    # Allow optional leading $ for tickers ($NVDA) and treat & / . inside aliases safely.
    pattern = re.compile(rf"(?<![A-Za-z0-9])\$?{escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    _ALIAS_PATTERN_CACHE[alias] = pattern
    return pattern


def whole_word_match(alias: str, text: str) -> bool:
    if not alias or not text:
        return False
    return _alias_pattern(alias).search(text) is not None


def relevance_score(
    item: dict,
    ticker: str,
    watched_tickers: list[str] | set[str] | None = None,
) -> int:
    """Score how strongly a Finnhub news item relates to ticker."""
    headline = (item.get("headline") or "").strip()
    summary = (item.get("summary") or "").strip()
    if not headline and not summary:
        return 0

    score = 0
    headline_hits = 0
    summary_hits = 0
    for alias in aliases_for(ticker):
        if whole_word_match(alias, headline):
            headline_hits += 1
        elif whole_word_match(alias, summary):
            summary_hits += 1

    score += min(headline_hits, 2) * HEADLINE_ALIAS_POINTS
    score += min(summary_hits, 2) * SUMMARY_ALIAS_POINTS

    # Extra nudge when the bare ticker symbol appears in the headline.
    if whole_word_match(ticker, headline):
        score += TICKER_SYMBOL_BONUS

    own_hit = score > 0
    watched = {t.strip().upper() for t in (watched_tickers or []) if t}
    watched.discard(ticker.strip().upper())
    if not own_hit and watched:
        for other in watched:
            if any(whole_word_match(alias, headline) for alias in aliases_for(other)):
                score -= RIVAL_PENALTY
                break

    return score


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


def parse_ticker_catalog(options: list | tuple) -> list[str]:
    """Parse all valid tickers from Brevo multiCategoryOptions (no per-user cap)."""
    seen: set[str] = set()
    result: list[str] = []
    for item in options:
        ticker = str(item).strip().upper()
        if not ticker or ticker in seen:
            continue
        if not TICKER_PATTERN.match(ticker):
            continue
        seen.add(ticker)
        result.append(ticker)
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


def unix_to_local(unix_ts: int | float) -> datetime:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone()


def local_timezone_label(dt: datetime) -> str:
    return (dt.strftime("%Z") or dt.tzname() or "").strip()


def format_local_time_label(dt: datetime) -> str:
    local = dt.astimezone()
    tz = local_timezone_label(local)
    time_label = local.strftime("%H:%M")
    return f"{time_label} {tz}".strip() if tz else time_label


def format_fetched_at_label(dt: datetime) -> str:
    local = dt.astimezone()
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    tz = local_timezone_label(local)
    time_label = f"{hour}:{local.strftime('%M')} {ampm}"
    if tz:
        time_label = f"{time_label} {tz}"
    return f"Fetched on {time_label}"


def format_local_datetime_label(dt: datetime) -> str:
    local = dt.astimezone()
    tz = local_timezone_label(local)
    base = local.strftime("%d %b %Y, %H:%M")
    return f"{base} {tz}".strip() if tz else base


def format_full_datetime(unix_ts: int | float | None) -> str:
    if not unix_ts:
        return ""
    published = unix_to_local(unix_ts)
    hour = published.hour % 12 or 12
    ampm = "AM" if published.hour < 12 else "PM"
    tz = local_timezone_label(published)
    base = f"{published.day} {published.strftime('%b %Y')}, {hour}:{published.strftime('%M')} {ampm}"
    return f"{base} {tz}".strip() if tz else base


def format_relative_time(unix_ts: int | float | None) -> str:
    if not unix_ts:
        return ""
    published = unix_to_local(unix_ts)
    now = datetime.now().astimezone()
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


def _build_story_dict(
    item: dict,
    score: int,
    *,
    excerpt: bool,
    relevance_fallback: bool,
) -> dict:
    summary = item.get("summary", "").strip()
    return {
        "headline": item.get("headline", "No headline").strip(),
        "summary": excerpt_summary(summary) if excerpt and summary else summary,
        "image": sanitize_article_image(item.get("image")),
        "url": item.get("url", "").strip(),
        "source": item.get("source", "").strip() or "News",
        "relative_time": format_relative_time(item.get("datetime")),
        "published_at": format_full_datetime(item.get("datetime")),
        "relevance_score": score,
        "relevance_fallback": relevance_fallback,
    }


def select_stories(
    news: list[dict],
    seen_stories: set[str],
    limit: int = HEADLINES_PER_TICKER,
    *,
    excerpt: bool = True,
    ticker: str | None = None,
    watched_tickers: list[str] | set[str] | None = None,
    min_score: int = MIN_RELEVANCE_SCORE,
    min_stories: int = MIN_STORIES_PER_TICKER,
    allow_fallback: bool = True,
) -> list[dict]:
    ranked: list[tuple[int, int, dict]] = []
    for index, item in enumerate(news):
        key = story_dedupe_key(item)
        if not key or key in seen_stories:
            continue

        score = 0
        if ticker:
            score = relevance_score(item, ticker, watched_tickers)
        ranked.append((score, index, item))

    # Higher relevance first; preserve Finnhub order on ties.
    ranked.sort(key=lambda row: (-row[0], row[1]))

    stories: list[dict] = []
    selected_keys: set[str] = set()

    def take_from(
        pool: list[tuple[int, int, dict]],
        target_count: int,
        *,
        fallback: bool,
    ) -> None:
        for score, _index, item in pool:
            if len(stories) >= target_count:
                return
            key = story_dedupe_key(item)
            if not key or key in seen_stories or key in selected_keys:
                continue
            selected_keys.add(key)
            seen_stories.add(key)
            stories.append(
                _build_story_dict(
                    item,
                    score,
                    excerpt=excerpt,
                    relevance_fallback=fallback,
                )
            )

    strong = [(score, index, item) for score, index, item in ranked if score >= min_score]
    take_from(strong, limit, fallback=False)

    used_fallback = False
    if allow_fallback and len(stories) < min_stories:
        # Pad with any score (including negative) so each ticker still gets
        # a minimum number of headlines when strong matches are scarce.
        before = len(stories)
        take_from(ranked, min_stories, fallback=True)
        used_fallback = len(stories) > before

    if used_fallback and stories and ticker and excerpt:
        print(
            f"Relevance fill: {ticker} padded to {len(stories)} story(ies) "
            f"(lowest score={min(s['relevance_score'] for s in stories)})"
        )

    return stories


def select_web_stories(
    news: list[dict],
    limit: int = WEB_HEADLINES_PER_TICKER,
    *,
    ticker: str | None = None,
    watched_tickers: list[str] | set[str] | None = None,
) -> list[dict]:
    return select_stories(
        news,
        set(),
        limit=limit,
        excerpt=False,
        ticker=ticker,
        watched_tickers=watched_tickers,
    )


def collect_digest_data(tickers: list[str], api_key: str) -> tuple[list[dict], int]:
    seen_stories: set[str] = set()
    sections: list[dict] = []
    total_stories = 0
    watched = list(tickers)

    # First pass: fetch quotes/logos/news for every ticker.
    raw_news: dict[str, list[dict]] = {}
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
            raw_news[ticker] = fetch_news(ticker, api_key)
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch news for {ticker}: {exc}", file=sys.stderr)
            section["error"] = str(exc)
            raw_news[ticker] = []

        sections.append(section)

    # Assign each article to the highest-scoring watched ticker that received it.
    best_owner: dict[str, tuple[str, int]] = {}
    for ticker, news in raw_news.items():
        for item in news:
            key = story_dedupe_key(item)
            if not key:
                continue
            score = relevance_score(item, ticker, watched)
            previous = best_owner.get(key)
            if previous is None or score > previous[1]:
                best_owner[key] = (ticker, score)

    for section in sections:
        ticker = section["ticker"]
        news = raw_news.get(ticker, [])
        # Keep articles this ticker owns, or that another ticker only weakly claimed.
        # Strong claims (score >= MIN_RELEVANCE_SCORE) still transfer away.
        owned_news = []
        for item in news:
            key = story_dedupe_key(item)
            owner = best_owner.get(key)
            if (
                owner
                and owner[0] != ticker
                and owner[1] >= MIN_RELEVANCE_SCORE
            ):
                continue
            owned_news.append(item)

        if section["error"] and not owned_news:
            continue

        stories = select_stories(
            owned_news,
            seen_stories,
            ticker=ticker,
            watched_tickers=watched,
        )
        section["stories"] = stories
        section["web_stories"] = select_web_stories(
            owned_news,
            ticker=ticker,
            watched_tickers=watched,
        )
        section["hero_image"] = next((story["image"] for story in stories if story["image"]), None)
        total_stories += len(stories)
        if news and not stories:
            print(
                f"Relevance: no usable stories for {ticker} "
                f"(fetched {len(news)}, owned {len(owned_news)})"
            )
        else:
            strong_count = sum(
                1 for story in section["web_stories"] if not story.get("relevance_fallback")
            )
            print(
                f"Relevance: {ticker} kept {len(section['web_stories'])}/{len(news)} "
                f"story(ies) ({strong_count} strong, owned {len(owned_news)})"
            )

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
        line += f"\nUpdate your tickers: {SITE_URL}/#update-tickers"
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


def format_email_heading(layout: dict) -> str:
    hero = layout.get("hero")
    if not hero:
        return DIGEST_HEADING
    ticker = hero["ticker"]
    quote = hero.get("quote")
    if not quote or quote.get("change_pct") is None:
        return DIGEST_HEADING
    change_pct = quote["change_pct"]
    if change_pct > 0:
        return f"{ticker} moved high"
    if change_pct < 0:
        return f"{ticker} moved low"
    return f"{ticker} held steady"


def digest_session(
    now: datetime | None = None, override: str = "auto"
) -> str:
    """Return 'pre_open' or 'post_close' from ET clock or an explicit override."""
    if override in ("pre_open", "post_close"):
        return override

    current = (now or datetime.now(ET_ZONE)).astimezone(ET_ZONE)
    local_time = current.time()
    if local_time < MARKET_OPEN_ET:
        return "pre_open"
    if local_time >= MARKET_CLOSE_ET:
        return "post_close"
    return "pre_open" if current.hour < 12 else "post_close"


def truncate_subject_snippet(text: str, max_len: int = HEADLINE_SNIPPET_LEN) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rstrip(" ,;:-")
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: max_len - 1].rstrip()) + "…"


def mover_subject_chip(mover: dict) -> str | None:
    change_pct = mover.get("change_pct")
    if change_pct is None:
        return None
    if change_pct > 0:
        emoji = "📈"
    elif change_pct < 0:
        emoji = "📉"
    else:
        emoji = "➡️"
    return f"{emoji} {mover['ticker']} {change_pct:+.1f}%"


def join_subject_chips(chips: list[str], *, prefix: str = "") -> str | None:
    if not chips:
        return None
    while chips:
        body = " · ".join(chips)
        subject = f"{prefix}{body}" if prefix else body
        if len(subject) <= SUBJECT_MAX_LEN:
            return subject
        chips = chips[:-1]
    return None


def format_pre_open_subject(layout: dict, date_label: str) -> str:
    sections: list[dict] = []
    hero = layout.get("hero")
    if hero:
        sections.append(hero)
    sections.extend(layout.get("compact") or [])

    chips: list[str] = []
    for section in sections:
        stories = section.get("stories") or []
        if not stories:
            continue
        headline = (stories[0].get("headline") or "").strip()
        if not headline:
            continue
        snippet = truncate_subject_snippet(headline)
        chips.append(f"{section['ticker']} · {snippet}")
        if len(chips) >= MAX_SUBJECT_HEADLINES:
            break

    subject = join_subject_chips(chips, prefix="💡 ")
    return subject or f"📊 Tickr Digest · {date_label}"


def format_post_close_subject(layout: dict, date_label: str) -> str:
    chips: list[str] = []
    for mover in layout.get("movers_bar") or []:
        chip = mover_subject_chip(mover)
        if not chip:
            continue
        chips.append(chip)
        if len(chips) >= MAX_SUBJECT_MOVERS:
            break

    subject = join_subject_chips(chips)
    return subject or f"📊 Tickr Digest · {date_label}"


def format_email_subject(layout: dict, date_label: str, session: str) -> str:
    if session == "pre_open":
        return format_pre_open_subject(layout, date_label)
    return format_post_close_subject(layout, date_label)


def build_email_digest(
    sections: list[dict],
    tickers: list[str],
    total_stories: int,
    session: str,
) -> tuple[str, str, str]:
    today_label = date.today().strftime("%d %b %Y")
    env = get_jinja_env()
    template = env.get_template("email_digest.html")
    layout = prepare_email_layout(sections)
    email_heading = format_email_heading(layout)
    subject = format_email_subject(layout, today_label, session)
    html = template.render(
        date_label=today_label,
        ticker_count=len(tickers),
        story_count=total_stories,
        site_url=SITE_URL,
        email_heading=email_heading,
        **layout,
    )
    text = build_plain_text(
        layout, today_label, len(tickers), total_stories, email_heading=email_heading
    )
    return html, text, subject


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
    layout: dict,
    date_label: str,
    ticker_count: int,
    story_count: int,
    *,
    email_heading: str | None = None,
) -> str:
    summary = layout["market_summary"]
    title = email_heading or f"Tickr Digest · {date_label}"
    lines = [
        title,
        f"{date_label} · {ticker_count} tickers · {story_count} stories",
        f"{summary['gainers']} up · {summary['losers']} down · {summary['flat']} flat",
        "",
    ]

    if layout["top_mover_label"]:
        lines.append(f"Today's biggest move: {layout['top_mover_label']}")
        lines.append("")

    if layout["hero"]:
        lines.append("=== BIGGEST MOVER ===")
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
    fetched_at_label: str | None = None,
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
        fetched_at_label=fetched_at_label,
        archives=archives,
        archive_href_prefix=archive_href_prefix,
        visible_story_count=HEADLINES_PER_TICKER,
        digest_heading=DIGEST_HEADING,
        subscribe_form_url=BREVO_SUBSCRIBE_FORM_URL or None,
        **layout,
    )


def parse_archive_filename(path: Path) -> datetime | None:
    stem = path.stem
    try:
        utc_dt = datetime.strptime(stem, "%Y-%m-%d-%H%M").replace(tzinfo=timezone.utc)
        return utc_dt.astimezone()
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
                "label": format_local_datetime_label(generated_at),
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
    generated_at_utc = datetime.now(timezone.utc)
    generated_at_local = generated_at_utc.astimezone()
    archive_slug = generated_at_utc.strftime("%Y-%m-%d-%H%M")
    fetched_at_label = format_fetched_at_label(generated_at_local)

    html = build_web_digest(sections, tickers, fetched_at_label=fetched_at_label)
    archive_html = build_web_digest(
        sections,
        tickers,
        is_archive=True,
        fetched_at_label=fetched_at_label,
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


def fetch_brevo_ticker_catalog(api_key: str) -> list[str]:
    headers = {"api-key": api_key, "Accept": "application/json"}
    response = requests.get(
        "https://api.brevo.com/v3/contacts/attributes",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    for attribute in response.json().get("attributes", []):
        if attribute.get("name", "").upper() != BREVO_TICKERS_ATTRIBUTE:
            continue
        options = attribute.get("multiCategoryOptions") or []
        tickers = parse_ticker_catalog(options)
        if tickers:
            return tickers
    return []


def send_email(
    html: str,
    text: str,
    api_key: str,
    sender_email: str,
    recipients: list[str],
    sender_name: str,
    subject: str,
) -> None:
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


def resolve_web_tickers(api_key: str | None, subscribers: list[dict]) -> list[str]:
    if api_key:
        try:
            catalog = fetch_brevo_ticker_catalog(api_key)
            if catalog:
                print(f"Using {len(catalog)} ticker(s) from Brevo TICKERS catalog")
                return catalog
            print(
                "Warning: Brevo TICKERS catalog empty or missing; falling back",
                file=sys.stderr,
            )
        except requests.RequestException as exc:
            print(f"Warning: failed to fetch Brevo ticker catalog: {exc}", file=sys.stderr)

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
    parser.add_argument(
        "--session",
        choices=("auto", "pre_open", "post_close"),
        default="auto",
        help=(
            "Email subject style: pre_open uses multi-headline teasers, "
            "post_close uses multi-mover chips. Default auto uses America/New_York clock."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build digests and print subjects without sending email or WhatsApp.",
    )
    parser.add_argument(
        "--recipient",
        metavar="EMAIL",
        help="Only prepare/send email for this subscriber address (case-insensitive).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    send_email_digest = args.email or args.all
    send_whatsapp_digest = args.whatsapp or args.all
    dry_run = args.dry_run
    recipient_filter = (args.recipient or "").strip().lower() or None
    email_session = digest_session(override=args.session)

    if recipient_filter and not send_email_digest:
        print(
            "Error: --recipient requires --email (or --all).",
            file=sys.stderr,
        )
        sys.exit(1)

    finnhub_key = require_env("FINNHUB_API_KEY", FINNHUB_KEY)

    subscribers: list[dict] = []
    if BREVO_LIST_ID and BREVO_API_KEY:
        subscribers = fetch_subscribers_with_tickers(int(BREVO_LIST_ID), BREVO_API_KEY)
        print(f"Fetched {len(subscribers)} subscriber(s) from Brevo list {BREVO_LIST_ID}")

    if recipient_filter and subscribers:
        matched = [
            s
            for s in subscribers
            if s.get("email", "").strip().lower() == recipient_filter
        ]
        if not matched:
            print(
                f"No subscriber matched --recipient {args.recipient}",
                file=sys.stderr,
            )
            sys.exit(1)
        subscribers = matched
        print(f"Filtered to recipient: {subscribers[0]['email']}")

    if dry_run and recipient_filter:
        # Narrow fetch to the recipient's tickers; skip publishing the public web digest.
        user_tickers = subscribers[0].get("tickers") if subscribers else []
        if not user_tickers:
            print(
                f"Skipped {args.recipient}: no valid tickers",
                file=sys.stderr,
            )
            sys.exit(1)
        digest_tickers = user_tickers
        print(
            f"Dry-run fetch limited to {len(digest_tickers)} ticker(s): "
            f"{', '.join(digest_tickers)}"
        )
        sections, _ = collect_digest_data(digest_tickers, finnhub_key)
        print("Web digest publish skipped (dry-run with --recipient).")
    else:
        digest_tickers = resolve_web_tickers(BREVO_API_KEY, subscribers)
        sections, _ = collect_digest_data(digest_tickers, finnhub_key)
        write_web_pages(sections, digest_tickers)

    if send_whatsapp_digest:
        if dry_run:
            print("WhatsApp dry-run: skipping send.")
        else:
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
        print(f"Email subject session: {email_session}")
        if dry_run:
            print("Email dry-run: subjects will be printed, nothing will be sent.")
        try:
            if BREVO_LIST_ID:
                if not subscribers and BREVO_API_KEY:
                    subscribers = fetch_subscribers_with_tickers(
                        int(BREVO_LIST_ID), BREVO_API_KEY
                    )
                    if recipient_filter:
                        subscribers = [
                            s
                            for s in subscribers
                            if s.get("email", "").strip().lower() == recipient_filter
                        ]
                        if not subscribers:
                            print(
                                f"No subscriber matched --recipient {args.recipient}",
                                file=sys.stderr,
                            )
                            sys.exit(1)
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
                        html, text, subject = build_email_digest(
                            user_sections, user_tickers, user_story_count, email_session
                        )
                        print(
                            f"Prepared email for {email} "
                            f"({len(user_tickers)} tickers, subject: {subject})"
                        )
                        if dry_run:
                            print(f"Dry-run subject [{email_session}]: {subject}")
                            print(f"Dry-run HTML size: {len(html)} chars")
                            sent_count += 1
                            continue
                        send_email(
                            html,
                            text,
                            BREVO_API_KEY,
                            EMAIL_FROM,
                            [email],
                            EMAIL_FROM_NAME,
                            subject,
                        )
                        sent_count += 1
                    if sent_count:
                        if dry_run:
                            print(f"Dry-run prepared {sent_count} personalized email(s)")
                        else:
                            print(f"Sent {sent_count} personalized email(s)")
            elif EMAIL_TO:
                if recipient_filter and EMAIL_TO.strip().lower() != recipient_filter:
                    print(
                        f"No subscriber matched --recipient {args.recipient}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                fallback_tickers = load_tickers()
                email_sections = (
                    filter_sections(sections, fallback_tickers)
                    if digest_tickers != fallback_tickers
                    else sections
                )
                email_story_count = count_email_stories(email_sections)
                html, text, subject = build_email_digest(
                    email_sections, fallback_tickers, email_story_count, email_session
                )
                print(
                    f"Prepared email for {EMAIL_TO} "
                    f"(subject: {subject}, {len(html)} chars HTML)"
                )
                if dry_run:
                    print(f"Dry-run subject [{email_session}]: {subject}")
                else:
                    send_email(
                        html,
                        text,
                        BREVO_API_KEY,
                        EMAIL_FROM,
                        [EMAIL_TO],
                        EMAIL_FROM_NAME,
                        subject,
                    )
            else:
                print("No email recipients found; skipping email send.")
        except requests.RequestException as exc:
            print(f"Warning: failed to send email: {exc}", file=sys.stderr)
    else:
        print("Email skipped (pass --email to enable).")


if __name__ == "__main__":
    main()
