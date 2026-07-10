"""GitHub Trending parser — scrapes the HTML listing (no official API).

Selectors are the single point of fragility for this source; keep them all
here so a layout change is a one-file fix. A layout change that yields zero
rows raises ScoutParserError → the target lands in targets_failed instead of
silently reporting "nothing new".
"""

from __future__ import annotations

import re

from services.http_client import PoliteHttpClient
from services.scout.models import CrawledItem, ScoutParserError, ScoutTarget

NAME = "github_trending"
TITLE = "GitHub Trending"
SOURCE = "github"  # throttle key
MIN_INTERVAL = 2.0

_WS_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def fetch(client: PoliteHttpClient, target: ScoutTarget) -> list[CrawledItem]:
    from bs4 import BeautifulSoup

    response = client.get(target.url, source=SOURCE)
    soup = BeautifulSoup(response.text, "lxml")

    rows = soup.select("article.Box-row")
    if not rows:
        raise ScoutParserError(
            "GitHub Trending: no article.Box-row found — page layout likely changed."
        )

    items: list[CrawledItem] = []
    for row in rows:
        link = row.select_one("h2 a[href]")
        if link is None:
            continue
        href = str(link["href"]).strip()
        repo = href.strip("/")  # "/owner/repo" → "owner/repo"
        if not repo:
            continue
        url = f"https://github.com/{repo}"

        desc_el = row.select_one("p")
        snippet = _squash(desc_el.get_text()) if desc_el else ""

        lang_el = row.select_one('span[itemprop="programmingLanguage"]')
        stars_el = row.select_one('a[href$="/stargazers"]')
        stats_parts = []
        if lang_el:
            stats_parts.append(_squash(lang_el.get_text()))
        if stars_el:
            stats_parts.append(f"⭐{_squash(stars_el.get_text())}")
        stats = " · ".join(stats_parts)

        items.append(
            CrawledItem(
                title=repo,
                url=url,
                dedupe_key=url,
                snippet=snippet,
                stats=stats,
            )
        )

    if not items:
        raise ScoutParserError(
            "GitHub Trending: rows found but none parsed — selectors likely stale."
        )
    return items
