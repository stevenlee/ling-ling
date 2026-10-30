"""Generic item-content extraction — fetch an item's URL, return readable text.

Feeds the per-item LLM analysis. Deliberately best-effort: any failure
(network, non-HTML, paywall junk) returns "" and the summary falls back to
title+snippet — a dead link must never break the digest.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from core.config import SCOUT_NEWS_MIN_CONTENT_CHARS
from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem
from services.scout.google_news import is_google_news_url, resolve_article_url

MAX_CONTENT_CHARS = 8000

# Article fetches use a browser profile: news-site WAFs 403 unknown bot UAs
# (investors.com, sciencedirect — observed live 2026-07-11). Same precedent
# as research_pipeline fetching FPO with a browser UA; listing/API fetches
# (GH/HN/arXiv) keep the honest Scout UA. PoliteHttpClient lets caller
# headers win over its default.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
}

_WS_RE = re.compile(r"[ \t]*\n[ \t\n]*")
_SPACES_RE = re.compile(r"[ \t]{2,}")

# Boilerplate containers stripped before text extraction (Phase 2 upgrades
# this to a proper readability extractor, e.g. trafilatura).
_STRIP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer", "form")


# Match access UI at the beginning of a block, not mentions of subscriptions
# within reporting. A story about paywalls must remain readable.
_NEWS_ACCESS_PROMPT = re.compile(
    r"^(?:subscribe (?:to (?:continue|read|unlock|our newsletter)|now|for (?:full|unlimited) access)"
    r"|sign (?:in|up) to (?:continue|read|access)"
    r"|already (?:a |an )?(?:subscriber|member)"
    r"|(?:please )?enable javascript|verify (?:that )?you are human"
    r"|access denied|checking your browser|just a moment|before you continue to google)",
    re.IGNORECASE,
)
_NEWS_UI_SELECTORS = (
    'aside, button, input, select, textarea, dialog, [role="dialog"], '
    '[hidden], [aria-hidden="true"], .cookie-banner, .cookie-consent, '
    ".newsletter-signup, .subscription-prompt, .paywall-message, .paywall"
)


_REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "m.reddit.com", "new.reddit.com")


def normalize_fetch_url(url: str) -> str:
    """Rewrite hosts whose default page is JS-only onto a server-rendered
    twin. Reddit's shreddit UI yields zero extractable text; old.reddit.com
    is plain HTML with the post + comments."""
    parts = urlparse(url)
    if (parts.hostname or "").lower() in _REDDIT_HOSTS:
        return parts._replace(netloc="old.reddit.com").geturl()
    return url


def fetch_item_content(client: PoliteHttpClient, item: CrawledItem) -> str:
    """Fetch item.url and extract readable text (bounded). "" on any failure."""
    if item.content:
        return item.content[:MAX_CONTENT_CHARS]

    google_news = is_google_news_url(item.url)
    if google_news:
        try:
            item.url = resolve_article_url(client, item.url, dict(BROWSER_HEADERS))
        except Exception as e:
            logging.warning("Scout: Google News 原文網址解析失敗 %s: %s", item.url, e)
            return ""

    url = normalize_fetch_url(item.url)
    source = (urlparse(url).hostname or "item").lower()
    try:
        response = client.get(
            url, source=source, headers=dict(BROWSER_HEADERS), timeout=15, retries=1
        )
    except requests.exceptions.RequestException as e:
        logging.info(f"Scout: content fetch failed for {item.url}: {e}")
        return ""

    if is_google_news_url(getattr(response, "url", url)):
        return ""

    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    if content_type and "html" not in content_type.lower():
        return ""  # PDFs, images, feeds — nothing extractable here yet

    try:
        text = extract_text(response.text, news=google_news)
        if google_news and not text:
            logging.info("Scout: 新聞正文不足或只有存取提示，改用標題與摘錄：%s", item.url)
        return text
    except Exception as e:
        logging.info(f"Scout: content extraction failed for {item.url}: {e}")
        return ""


def extract_text(html: str, *, max_chars: int = MAX_CONTENT_CHARS, news: bool = False) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    if news:
        for tag in soup.select(_NEWS_UI_SELECTORS):
            tag.decompose()
        for tag in soup.find_all(["p", "h1", "h2", "h3", "li"]):
            if _NEWS_ACCESS_PROMPT.match(tag.get_text(" ", strip=True)):
                tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    if news and not soup.find("article"):
        heading = root.find("h1")
        if heading and re.match(
            r"^(?:subscribe|sign in|newsletter)\b", heading.get_text(" ", strip=True), re.I
        ):
            return ""
    text = root.get_text(separator="\n")
    text = _SPACES_RE.sub(" ", text)
    text = _WS_RE.sub("\n", text).strip()
    text = text[:max_chars]
    if news:
        lines = [line for line in text.splitlines() if not _NEWS_ACCESS_PROMPT.match(line)]
        text = "\n".join(lines).strip()
        # Repeated navigation or access messages cannot inflate sufficiency.
        unique = " ".join(dict.fromkeys(lines))
        if sum(char.isalnum() for char in unique) < SCOUT_NEWS_MIN_CONTENT_CHARS:
            return ""
    return text
