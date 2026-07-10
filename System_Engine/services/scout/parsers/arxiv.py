"""arXiv parser — a /list/<category>/recent URL is served via the official API.

Same integration research_pipeline uses (Atom XML, 3s politeness per arXiv's
API terms). The abstract goes straight into ``content``, so digest skips the
per-item page fetch for arXiv entries.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem, ScoutParserError, ScoutTarget

NAME = "arxiv"
TITLE = "arXiv (recent)"
SOURCE = "arxiv"  # throttle key
MIN_INTERVAL = 3.0

_LIST_URL_RE = re.compile(r"arxiv\.org/list/([A-Za-z0-9.\-]+)/")
_VERSION_RE = re.compile(r"v\d+$")
_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_API_URL = (
    "https://export.arxiv.org/api/query?search_query=cat:{cat}"
    "&sortBy=submittedDate&sortOrder=descending&start=0&max_results={n}"
)


def _category(target: ScoutTarget) -> str:
    match = _LIST_URL_RE.search(target.url)
    if not match:
        raise ScoutParserError(
            f"arXiv: expected a https://arxiv.org/list/<category>/... URL, got {target.url}"
        )
    return match.group(1)


def section_title(target: ScoutTarget) -> str:
    try:
        return f"arXiv {_category(target)} (recent)"
    except ScoutParserError:
        return TITLE


def fetch(client: PoliteHttpClient, target: ScoutTarget) -> list[CrawledItem]:
    cat = _category(target)
    limit = target.max_items or 10
    response = client.get(_API_URL.format(cat=cat, n=limit), source=SOURCE)
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as e:
        raise ScoutParserError(f"arXiv: API returned unparseable XML: {e}") from e

    items: list[CrawledItem] = []
    for entry in root.findall("a:entry", _ATOM):
        title = _text(entry, "a:title")
        abs_url = _text(entry, "a:id")
        if not title or not abs_url:
            continue
        abstract = _text(entry, "a:summary")
        authors = [_text(author, "a:name") for author in entry.findall("a:author", _ATOM)]
        authors = [a for a in authors if a]
        stats = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")

        items.append(
            CrawledItem(
                title=title,
                url=abs_url,
                # v1→v2 revisions are the same paper — strip the version.
                dedupe_key=_VERSION_RE.sub("", abs_url),
                snippet=abstract[:300],
                stats=stats,
                content=abstract,  # abstract IS the content; no page fetch needed
            )
        )

    if not items:
        raise ScoutParserError(f"arXiv: no entries returned for cat:{cat}.")
    return items


def _text(element: ET.Element, tag: str) -> str:
    node = element.find(tag, _ATOM)
    return re.sub(r"\s+", " ", node.text).strip() if node is not None and node.text else ""
