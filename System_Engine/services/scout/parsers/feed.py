"""Generic parser — any blog/news site, via its RSS/Atom feed.

The target URL may be the feed itself, or an HTML page carrying a
`<link rel="alternate" type="application/rss+xml|atom+xml">` autodiscovery
tag (the long-standing convention nearly every blog/news engine emits).
Feeds are parsed by hand with xml.etree (RSS 2.0 + Atom) — no new
dependency. Sites with no discoverable feed fail visibly in the report.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem, ScoutParserError, ScoutTarget

NAME = "feed"
TITLE = "Feed"
SOURCE = "feed"  # per-call throttle actually keys on the target's host
MIN_INTERVAL = 1.0

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_XML_HINT_RE = re.compile(r"^\s*(<\?xml|<rss[\s>]|<feed[\s>])", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_FEED_TYPES = ("application/rss+xml", "application/atom+xml")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "feed").lower()


def section_title(target: ScoutTarget) -> str:
    return _host(target.url)


def fetch(client: PoliteHttpClient, target: ScoutTarget) -> list[CrawledItem]:
    response = client.get(target.url, source=_host(target.url))
    text = response.text

    if not _looks_like_feed(response, text):
        feed_url = _discover_feed_url(text, base_url=target.url)
        if not feed_url:
            raise ScoutParserError(
                f"{_host(target.url)}: 頁面沒有可探測的 RSS/Atom feed — 請改填該站的 feed URL"
            )
        text = client.get(feed_url, source=_host(target.url)).text

    items = parse_feed(text)
    if not items:
        raise ScoutParserError(f"{_host(target.url)}: feed 解析不出任何條目。")
    return items


def _looks_like_feed(response, text: str) -> bool:
    content_type = getattr(response, "headers", {}).get("Content-Type", "").lower()
    if "xml" in content_type and "xhtml" not in content_type:
        return True
    return bool(_XML_HINT_RE.match(text or ""))


def _discover_feed_url(html: str, *, base_url: str) -> str | None:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        if str(link.get("type", "")).lower() in _FEED_TYPES and link.get("href"):
            return urljoin(base_url, str(link["href"]))
    return None


# ── feed XML → items ───────────────────────────────────────────────────


def parse_feed(xml_text: str) -> list[CrawledItem]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ScoutParserError(f"feed XML 解析失敗: {e}") from e

    if root.tag == f"{_ATOM_NS}feed":
        return [item for entry in root.findall(f"{_ATOM_NS}entry") if (item := _atom_entry(entry))]
    if root.tag.lower() == "rss":
        channel = root.find("channel")
        entries = channel.findall("item") if channel is not None else []
        return [item for entry in entries if (item := _rss_item(entry))]
    raise ScoutParserError(f"不認得的 feed 根元素 <{root.tag}>（僅支援 RSS 2.0 / Atom）。")


def _rss_item(entry: ET.Element) -> CrawledItem | None:
    title = _plain(entry.findtext("title"))
    link = (entry.findtext("link") or "").strip()
    if not title or not link:
        return None
    guid = (entry.findtext("guid") or "").strip()
    return CrawledItem(
        title=title,
        url=link,
        dedupe_key=guid or link,
        snippet=_plain(entry.findtext("description"))[:300],
    )


def _atom_entry(entry: ET.Element) -> CrawledItem | None:
    title = _plain(entry.findtext(f"{_ATOM_NS}title"))
    link = ""
    for link_el in entry.findall(f"{_ATOM_NS}link"):
        href = str(link_el.get("href") or "").strip()
        if not href:
            continue
        if link_el.get("rel") in (None, "alternate"):
            link = href
            break
        link = link or href
    if not title or not link:
        return None
    entry_id = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
    snippet = _plain(entry.findtext(f"{_ATOM_NS}summary") or entry.findtext(f"{_ATOM_NS}content"))
    return CrawledItem(
        title=title,
        url=link,
        dedupe_key=entry_id or link,
        snippet=snippet[:300],
    )


def _plain(value: str | None) -> str:
    """Feed titles/descriptions may carry embedded HTML — flatten to text."""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()
