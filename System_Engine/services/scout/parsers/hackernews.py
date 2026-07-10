"""Hacker News (newest) parser — Algolia public API, not the HTML page.

`hn.algolia.com/api/v1/search_by_date?tags=story` is the stable, documented
way to read the /newest firehose; scraping news.ycombinator.com/newest would
break on markup changes and needs pagination for the same data.
"""

from __future__ import annotations

from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem, ScoutParserError, ScoutTarget

NAME = "hackernews"
TITLE = "Hacker News (newest)"
SOURCE = "hackernews"  # throttle key
MIN_INTERVAL = 1.0

_API_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage={n}"
_ITEM_URL = "https://news.ycombinator.com/item?id={id}"


def fetch(client: PoliteHttpClient, target: ScoutTarget) -> list[CrawledItem]:
    limit = target.max_items or 30
    response = client.get(_API_URL.format(n=limit), source=SOURCE)
    try:
        hits = response.json().get("hits", [])
    except ValueError as e:
        raise ScoutParserError(f"HN Algolia: non-JSON response: {e}") from e
    if not isinstance(hits, list):
        raise ScoutParserError("HN Algolia: unexpected payload shape (hits not a list).")

    items: list[CrawledItem] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        story_id = str(hit.get("objectID") or "").strip()
        if not title or not story_id:
            continue
        discussion_url = _ITEM_URL.format(id=story_id)
        # Ask HN / Show HN posts have no external url — link the discussion.
        url = str(hit.get("url") or "").strip() or discussion_url

        points = hit.get("points") or 0
        comments = hit.get("num_comments") or 0
        stats = f"▲{points} · 💬{comments}"

        items.append(
            CrawledItem(
                title=title,
                url=url,
                dedupe_key=discussion_url,  # story id is the stable identity
                snippet=str(hit.get("story_text") or "").strip()[:300],
                stats=stats,
            )
        )

    if not items:
        raise ScoutParserError("HN Algolia: response contained no parseable stories.")
    return items
