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

from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem

MAX_CONTENT_CHARS = 8000

_WS_RE = re.compile(r"[ \t]*\n[ \t\n]*")
_SPACES_RE = re.compile(r"[ \t]{2,}")

# Boilerplate containers stripped before text extraction (Phase 2 upgrades
# this to a proper readability extractor, e.g. trafilatura).
_STRIP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer", "form")


def fetch_item_content(client: PoliteHttpClient, item: CrawledItem) -> str:
    """Fetch item.url and extract readable text (bounded). "" on any failure."""
    if item.content:
        return item.content[:MAX_CONTENT_CHARS]

    source = (urlparse(item.url).hostname or "item").lower()
    try:
        response = client.get(item.url, source=source, timeout=15, retries=1)
    except requests.exceptions.RequestException as e:
        logging.info(f"Scout: content fetch failed for {item.url}: {e}")
        return ""

    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    if content_type and "html" not in content_type.lower():
        return ""  # PDFs, images, feeds — nothing extractable here yet

    try:
        return extract_text(response.text)
    except Exception as e:
        logging.info(f"Scout: content extraction failed for {item.url}: {e}")
        return ""


def extract_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text(separator="\n")
    text = _SPACES_RE.sub(" ", text)
    text = _WS_RE.sub("\n", text).strip()
    return text[:MAX_CONTENT_CHARS]
