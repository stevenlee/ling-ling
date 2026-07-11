"""Scout digest — crawl the targets list, analyze each item, write the report.

Entry point ``run_scout_digest(llm)`` is what the scheduler's scout_daily task
calls. Failure isolation is per target: a broken site lands in the report's
抓取狀況 section, it never kills the digest. LLM/content failures degrade
per item — the line falls back to the listing snippet instead of vanishing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from core.parsing.markdown_metadata import dump_markdown_with_metadata
from services.http_client import PoliteHttpClient
from services.scout import parsers
from services.scout.content import fetch_item_content
from services.scout.models import CrawledItem, ScoutParserError, TargetResult
from services.scout.state import ScoutState
from services.scout.targets import load_targets

WEEKLY_MIN_SECONDS = 6 * 86400  # "weekly" = at least 6 days since last crawl
STREAK_MIN_DAYS = 3  # 持續上榜 line lists items riding a list this many days
BRIDGE_TOP_K = 2  # related vault notes per item (P2.3)
# Max vector distance for a bridging hit. Conservative on purpose: a wrong
# [[link]] is worse than a missing one.
BRIDGE_MAX_DISTANCE = 0.45

_SUMMARIZE_SYSTEM = (
    "You are Scout, a reconnaissance assistant preparing a daily intelligence "
    "report. The user gives you ONE crawled item: title, source, stats, and the "
    "extracted page content (possibly truncated or missing). Write 2-4 sentences "
    "of {language} prose: what it actually is or says (grounded in the content, "
    "not just the title) and why it might matter. Plain prose only — no headers, "
    "bullets, or preamble."
)


@dataclass
class ScoutDigestResult:
    status: str
    summary: str
    report_path: Path | None = None


def run_scout_digest(
    llm,
    rag=None,
    *,
    targets_file: Path | None = None,
    state_file: Path | None = None,
    report_dir: Path | None = None,
    mirror_dir: Path | None = None,
    client: PoliteHttpClient | None = None,
    now: datetime | None = None,
) -> ScoutDigestResult:
    from core.config import (
        FROM_LLM_DIR,
        SCOUT_MIRROR_DIR,
        SCOUT_STATE_FILE,
        SCOUT_TARGETS_FILE,
        settings,
    )

    now = now or datetime.now()
    targets_file = targets_file or SCOUT_TARGETS_FILE
    state_file = state_file or SCOUT_STATE_FILE
    report_dir = report_dir or FROM_LLM_DIR
    mirror_dir = mirror_dir or SCOUT_MIRROR_DIR
    client = client or PoliteHttpClient(dict(parsers.MIN_INTERVALS), user_agent=_scout_user_agent())

    targets, file_language = load_targets(targets_file)
    if not targets:
        return ScoutDigestResult("skipped", f"No valid targets in {targets_file.name}.")
    language = file_language or getattr(settings, "SCOUT_LANGUAGE", "") or settings.OUTPUT_LANGUAGE
    default_max_items = getattr(settings, "SCOUT_MAX_ITEMS_PER_TARGET", 10)

    state = ScoutState(state_file)
    state.prune_seen(now=now)

    # NOTE (R5): seen-marks/streaks/crawl clocks accumulate IN MEMORY and are
    # persisted only after the report is safely on disk (or after a legitimate
    # no-new-items outcome). The daemon restarts often — an interrupted run
    # must not swallow items it never reported. Tradeoff accepted: a crash
    # mid-run re-crawls and re-pays the LLM analyses on the next run.
    results = [
        _crawl_target(target, client, state, now=now, default_max_items=default_max_items)
        for target in targets
    ]

    new_count = sum(len(r.items) for r in results)
    failed = [r for r in results if r.error]
    if new_count == 0:
        state.save()  # nothing to report — sightings/streaks/clocks are final
        note = f"; {len(failed)} target(s) failed" if failed else ""
        return ScoutDigestResult(
            "succeeded", f"No new items across {len(targets)} target(s){note}."
        )

    # Per-item: fetch the article/page content, then one analysis call each.
    # Both steps degrade item-by-item — a dead link or a blank LLM reply just
    # means that line falls back to the listing snippet.
    fetch_content = getattr(settings, "SCOUT_FETCH_CONTENT", True)
    bridging = rag is not None and getattr(settings, "SCOUT_BRIDGING", True)
    summaries: dict[int, dict[int, str]] = {}
    for r in results:
        if not r.items:
            continue
        per_target: dict[int, str] = {}
        for i, item in enumerate(r.items, start=1):
            if fetch_content and not item.content:
                domain = (urlparse(item.url).hostname or "").lower()
                if domain and not state.domain_blocked(domain, now=now):
                    item.content = fetch_item_content(client, item)
                    state.record_content_fetch(domain, ok=bool(item.content), now=now)
                item.content_missing = not item.content
            summary = _summarize_item(llm, r, item, language)
            if summary:
                per_target[i] = summary
            if bridging:
                item.related = _find_related_notes(rag, item, summary)
        summaries[id(r)] = per_target

    body = _render_body(now, results, summaries)
    mirror_to = mirror_dir if getattr(settings, "SCOUT_MIRROR", True) else None
    report_path = _write_report(report_dir, now, results, new_count, body, mirror_dir=mirror_to)
    state.save()  # report is on disk — NOW the seen-marks (and strikes) are true
    return ScoutDigestResult(
        "succeeded",
        f"Scout report written: {new_count} new item(s) from "
        f"{sum(1 for r in results if r.items)} target(s)"
        + (f"; {len(failed)} failed" if failed else "")
        + ".",
        report_path=report_path,
    )


def _scout_user_agent() -> str:
    return "LingLingScoutBot/1.0 (+https://github.com/stevenlee/ling-ling)"


# ── crawl ──────────────────────────────────────────────────────────────


def _crawl_target(
    target, client: PoliteHttpClient, state: ScoutState, *, now: datetime, default_max_items: int
) -> TargetResult:
    result = TargetResult(target=target, section_title=_section_title(target))

    if target.cadence == "weekly":
        last = state.last_crawled_at(target.url)
        if last is not None and (now - last).total_seconds() < WEEKLY_MIN_SECONDS:
            result.skipped_reason = "weekly：距上次不足 6 天"
            return result

    parser = parsers.resolve_parser(target)
    if parser is None:
        result.error = f"沒有對應的 parser（{target.parser or '自動偵測失敗'}）— 通用抽取待 Phase 2"
        return result
    # Parsers may localize the section title per target (arXiv: category).
    result.section_title = (
        parser.section_title(target) if hasattr(parser, "section_title") else parser.TITLE
    )

    try:
        items = parser.fetch(client, target)
    except (ScoutParserError, requests.exceptions.RequestException) as e:
        result.error = str(e)
        return result
    except Exception as e:  # a parser bug must not kill the whole digest
        logging.exception(f"Scout: parser {parser.NAME} crashed on {target.url}")
        result.error = f"parser 內部錯誤：{e}"
        return result

    items = items[: target.max_items or default_max_items]
    result.fetched_count = len(items)
    for item in items:
        is_new = not state.is_seen(item.dedupe_key)
        streak = state.record_sighting(item.dedupe_key, title=item.title, now=now)
        if is_new:
            result.items.append(item)
        elif streak >= STREAK_MIN_DAYS:
            result.streaks.append((item.title, item.url, streak))
    state.mark_crawled(target.url, now=now)
    return result


def _section_title(target) -> str:
    return target.url.removeprefix("https://").removeprefix("http://").rstrip("/")


# ── LLM: per-item analysis ─────────────────────────────────────────────


def _summarize_item(llm, result: TargetResult, item: CrawledItem, language: str) -> str:
    """One completion per item, grounded in the fetched content. "" on a blank
    LLM reply — the renderer then falls back to the listing snippet."""
    parts = [
        f"Source: {result.section_title}",
        f"Title: {item.title}",
        f"URL: {item.url}",
    ]
    if item.stats:
        parts.append(f"Stats: {item.stats}")
    if item.snippet:
        parts.append(f"Listing snippet: {item.snippet}")
    content = item.content or "(unavailable — judge from the title and snippet alone)"
    parts.append(f"Page content:\n{content}")

    raw = llm.complete(
        _SUMMARIZE_SYSTEM.format(language=language), "\n".join(parts), stage="scout_summarize"
    )
    summary = " ".join((raw or "").split())
    if not summary:
        logging.warning(f"Scout: empty summary for {item.url}")
    return summary


# ── RAG bridging (P2.3) ────────────────────────────────────────────────


def is_own_report(title: str) -> bool:
    """Scout/Dig 自家產出（含鏡射檔的 ✅ 前綴）不得成為『相關筆記』——
    首次實跑 [[✅Scout-2026-07-11]] 連回了當天日報自己。"""
    return title.lstrip("✅").strip().startswith(("Scout-", "Dig-"))


def _find_related_notes(rag, item: CrawledItem, summary: str) -> list[str]:
    """Vault-note titles genuinely related to this item (deterministic, no
    LLM). Conservative distance gate; own mirrored Scout reports excluded so
    yesterday's digest never becomes today's 'related note'. Fail-open."""
    query = f"{item.title}\n{summary or item.snippet}"[:500]
    try:
        hits = rag.query_notes(query, top_k=BRIDGE_TOP_K)
    except Exception as e:
        logging.warning(f"Scout: bridging query failed for {item.url}: {e}")
        return []
    titles: list[str] = []
    for hit in hits or []:
        meta = hit.get("metadata") or {}
        title = str(meta.get("title") or "").strip()
        distance = hit.get("distance")
        if not title or title in titles or is_own_report(title):
            continue
        if isinstance(distance, (int, float)) and distance > BRIDGE_MAX_DISTANCE:
            continue
        titles.append(title)
    return titles


# ── report ─────────────────────────────────────────────────────────────
# NOTE (R2): the cross-source "綜合分析" section was removed on user feedback —
# single-day trend synthesis over a handful of unrelated listings produced
# consultant filler. It returns in Phase 2 once it can be GROUNDED: cross-day
# streak signals + vault RAG bridging. See DesignDoc/Scout_implementation_plan.md.


def _render_item(item: CrawledItem, summary: str | None) -> str:
    line = f"- [{item.title}]({item.url})"
    if item.stats:
        line += f" {item.stats}"
    text = summary or item.snippet
    if text:
        line += f" — {text}"
    if item.content_missing:
        line += "（未取得內文，僅依標題與摘錄分析）"
    if item.related:
        line += "｜相關筆記: " + "、".join(f"[[{title}]]" for title in item.related)
    return line


def _render_body(now: datetime, results: list[TargetResult], summaries: dict) -> str:
    parts = [f"# 📓 調查兵團日報 {now.strftime('%Y-%m-%d')}", ""]

    for result in results:
        # A section renders when it has fresh items OR still-riding streaks —
        # "0 new but bun is on day 5" is itself a signal worth showing.
        if not result.items and not result.streaks:
            continue
        per_target = summaries.get(id(result), {})
        parts.append(f"## {result.section_title}")
        parts.append("")
        for i, item in enumerate(result.items, start=1):
            parts.append(_render_item(item, per_target.get(i)))
        if result.streaks:
            streak_bits = "、".join(
                f"[{title}]({url})（第 {days} 天）" for title, url, days in result.streaks
            )
            parts.append(f"- 🔁 持續上榜：{streak_bits}")
        parts.append("")

    parts.append("## 🧹 抓取狀況")
    parts.append("")
    for result in results:
        label = _section_title(result.target)
        if result.error:
            parts.append(f"- {label}：抓取失敗 — {result.error}")
        elif result.skipped_reason:
            parts.append(f"- {label}：跳過（{result.skipped_reason}）")
        else:
            parts.append(f"- {label}：{result.fetched_count} 項（{len(result.items)} 新）")
    parts.append("")
    return "\n".join(parts)


def _write_report(
    report_dir: Path,
    now: datetime,
    results: list[TargetResult],
    new_count: int,
    body: str,
    *,
    mirror_dir: Path | None = None,
) -> Path:
    date_str = now.strftime("%Y-%m-%d")
    metadata = {
        "title": f"Scout-{date_str}",
        "type": "Scout",
        "date_created": now.strftime("%Y-%m-%d %H:%M:%S"),
        "targets_ok": [r.target.url for r in results if r.ok and not r.skipped_reason],
        "targets_failed": [f"{r.target.url}: {r.error}" for r in results if r.error],
        "new_items": new_count,
        "tags": ["Scout"],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"✅Scout-{date_str}.md"
    full_markdown = dump_markdown_with_metadata(metadata, body)
    path.write_text(full_markdown, encoding="utf-8")
    # P2.4: fromLingLing/ is not RAG-indexed; a byte-identical mirror under
    # Notes/Scout/ is (VaultWatcher watches Notes/), making digests searchable.
    if mirror_dir is not None:
        try:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            (mirror_dir / path.name).write_text(full_markdown, encoding="utf-8")
        except Exception as e:
            logging.warning(f"Scout: mirror to {mirror_dir} failed: {e}")
    return path
