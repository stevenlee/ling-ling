"""Scout data model — targets, crawled items, per-target outcomes.

Kept dependency-free so parsers/state/digest can all import it without
cycles. See DesignDoc/Scout_implementation_plan.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ScoutParserError(Exception):
    """A parser could not extract items (page structure changed, bad payload).

    Raised instead of returning [] so digest can tell "site changed / broke"
    (→ targets_failed, visible in the report) apart from "genuinely nothing
    new today".
    """


@dataclass
class ScoutTarget:
    """One entry from the Scout.md targets list."""

    url: str
    parser: str | None = None  # explicit parser name; None → auto-detect by URL
    cadence: str = "daily"  # "daily" | "weekly"
    max_items: int | None = None  # None → settings.SCOUT_MAX_ITEMS_PER_TARGET


@dataclass
class CrawledItem:
    """One crawled entry, normalized across parsers.

    ``dedupe_key`` must be stable across days for the same underlying thing
    (repo URL, HN story id) — it is what the seen-state hashes.
    """

    title: str
    url: str
    dedupe_key: str
    snippet: str = ""
    stats: str = ""  # short human string, e.g. "⭐12,345" / "💬 42"
    # Extracted page/article text for the per-item LLM analysis. A parser that
    # already has it (arXiv abstract) fills it; otherwise digest fetches the
    # item URL and extracts it. "" → the summary falls back to title+snippet.
    content: str = ""


@dataclass
class TargetResult:
    """Outcome of crawling one target (feeds the report's 抓取狀況 section)."""

    target: ScoutTarget
    section_title: str = ""
    items: list[CrawledItem] = field(default_factory=list)  # new items only
    fetched_count: int = 0
    skipped_reason: str | None = None  # cadence gate etc. — not an error
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
