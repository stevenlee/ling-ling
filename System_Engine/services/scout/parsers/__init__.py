"""Scout parser registry + URL auto-detection.

A parser is a module exposing NAME / SOURCE / MIN_INTERVAL constants and
``fetch(client, target) -> list[CrawledItem]``. Explicit ``parser:`` in the
targets file wins; otherwise the URL host picks one. No generic-article
fallback yet (Phase 2) — an unmatched target fails visibly in the report.
"""

from __future__ import annotations

from types import ModuleType
from urllib.parse import urlparse

from services.scout.models import ScoutTarget
from services.scout.parsers import arxiv, github_trending, hackernews

PARSERS: dict[str, ModuleType] = {
    github_trending.NAME: github_trending,
    hackernews.NAME: hackernews,
    arxiv.NAME: arxiv,
}

# Politeness intervals for PoliteHttpClient, keyed by each parser's SOURCE.
MIN_INTERVALS: dict[str, float] = {mod.SOURCE: mod.MIN_INTERVAL for mod in PARSERS.values()}


def resolve_parser(target: ScoutTarget) -> ModuleType | None:
    """Explicit parser name first, then URL-based auto-detection."""
    if target.parser:
        return PARSERS.get(target.parser)

    host = (urlparse(target.url).hostname or "").lower()
    path = urlparse(target.url).path
    if host in ("github.com", "www.github.com") and path.startswith("/trending"):
        return github_trending
    if host == "news.ycombinator.com":
        return hackernews
    if host in ("arxiv.org", "www.arxiv.org", "export.arxiv.org") and path.startswith("/list/"):
        return arxiv
    return None
