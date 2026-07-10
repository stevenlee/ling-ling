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

import requests

from core.parsing.markdown_metadata import dump_markdown_with_metadata
from services.http_client import PoliteHttpClient
from services.scout import parsers
from services.scout.content import fetch_item_content
from services.scout.models import CrawledItem, ScoutParserError, TargetResult
from services.scout.state import ScoutState
from services.scout.targets import load_targets

WEEKLY_MIN_SECONDS = 6 * 86400  # "weekly" = at least 6 days since last crawl

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
    *,
    targets_file: Path | None = None,
    state_file: Path | None = None,
    report_dir: Path | None = None,
    client: PoliteHttpClient | None = None,
    now: datetime | None = None,
) -> ScoutDigestResult:
    from core.config import FROM_LLM_DIR, SCOUT_STATE_FILE, SCOUT_TARGETS_FILE, settings

    now = now or datetime.now()
    targets_file = targets_file or SCOUT_TARGETS_FILE
    state_file = state_file or SCOUT_STATE_FILE
    report_dir = report_dir or FROM_LLM_DIR
    client = client or PoliteHttpClient(dict(parsers.MIN_INTERVALS), user_agent=_scout_user_agent())

    targets, file_language = load_targets(targets_file)
    if not targets:
        return ScoutDigestResult("skipped", f"No valid targets in {targets_file.name}.")
    language = file_language or getattr(settings, "SCOUT_LANGUAGE", "") or settings.OUTPUT_LANGUAGE
    default_max_items = getattr(settings, "SCOUT_MAX_ITEMS_PER_TARGET", 10)

    state = ScoutState(state_file)
    state.prune_seen(now=now)

    results = [
        _crawl_target(target, client, state, now=now, default_max_items=default_max_items)
        for target in targets
    ]
    state.save()  # persist crawl clocks + seen marks even if the LLM below dies

    new_count = sum(len(r.items) for r in results)
    failed = [r for r in results if r.error]
    if new_count == 0:
        note = f"; {len(failed)} target(s) failed" if failed else ""
        return ScoutDigestResult(
            "succeeded", f"No new items across {len(targets)} target(s){note}."
        )

    # Per-item: fetch the article/page content, then one analysis call each.
    # Both steps degrade item-by-item — a dead link or a blank LLM reply just
    # means that line falls back to the listing snippet.
    fetch_content = getattr(settings, "SCOUT_FETCH_CONTENT", True)
    summaries: dict[int, dict[int, str]] = {}
    for r in results:
        if not r.items:
            continue
        per_target: dict[int, str] = {}
        for i, item in enumerate(r.items, start=1):
            if fetch_content and not item.content:
                item.content = fetch_item_content(client, item)
            summary = _summarize_item(llm, r, item, language)
            if summary:
                per_target[i] = summary
        summaries[id(r)] = per_target

    body = _render_body(now, results, summaries)
    report_path = _write_report(report_dir, now, results, new_count, body)
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
    result.items = [item for item in items if not state.is_seen(item.dedupe_key)]
    for item in result.items:
        state.mark_seen(item.dedupe_key, now=now)
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
    return line


def _render_body(now: datetime, results: list[TargetResult], summaries: dict) -> str:
    parts = [f"# 📓 Scout 日報 {now.strftime('%Y-%m-%d')}", ""]

    for result in results:
        if not result.items:
            continue
        per_target = summaries.get(id(result), {})
        parts.append(f"## {result.section_title}")
        parts.append("")
        for i, item in enumerate(result.items, start=1):
            parts.append(_render_item(item, per_target.get(i)))
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
    report_dir: Path, now: datetime, results: list[TargetResult], new_count: int, body: str
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
    path.write_text(dump_markdown_with_metadata(metadata, body), encoding="utf-8")
    return path
