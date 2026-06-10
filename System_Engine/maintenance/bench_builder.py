"""Bench builder — the retrieval bench grows itself as the vault grows.

For each indexed page that has facets and no bench coverage yet, the LLM
paraphrases the page's thesis into a natural question. The candidate case
must pass a quality gate — the CURRENT system has to answer it correctly —
before it's accepted. That's the regression-guard philosophy: auto cases
lock in today's working capability; any future change that breaks them is
a regression the daily bench will catch.

Auto cases live in `scratch/retrieval_bench_auto.yml`, separate from the
hand-written bench file, so rewrites never clobber manual cases (or their
comments). Delete the auto file at any time to reset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from core.config import (
    BENCH_AUTO_MAX_CASES,
    BENCH_AUTO_PER_RUN,
    RETRIEVAL_BENCH_AUTO_FILE,
    RETRIEVAL_BENCH_FILE,
)
from maintenance.retrieval_bench import _matches_expected, load_bench_cases


@dataclass
class BenchBuilderResult:
    status: str                  # "succeeded" | "skipped"
    message: str
    added: list[dict] = field(default_factory=list)
    rejected: int = 0            # candidates that failed the quality gate


def _covered_titles(cases: list[dict]) -> set[str]:
    covered: set[str] = set()
    for case in cases:
        for key in ("expected_top_1", "expected_top_k"):
            value = case.get(key)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            covered.update(str(v) for v in values if v)
    return covered


def _thesis_by_title(facet_entries: list[dict]) -> dict[str, str]:
    """Pick each title's primary facet (facet_index 0 = thesis), newest first."""
    by_title: dict[str, tuple] = {}
    for entry in facet_entries:
        title = entry.get("title")
        text = (entry.get("text") or "").strip()
        if not title or not text:
            continue
        rank = (entry.get("facet_index") or 0, str(entry.get("timestamp") or ""))
        current = by_title.get(title)
        if current is None or rank[0] < current[0][0]:
            by_title[title] = (rank, text)
    # Newest documents first so fresh ingestions gain coverage soonest.
    ordered = sorted(by_title.items(), key=lambda kv: kv[1][0][1], reverse=True)
    return {title: text for title, (_, text) in ordered}


def build_bench_cases(
    rag,
    llm,
    *,
    bench_path: Path = None,
    auto_path: Path = None,
    max_total: int = None,
    per_run: int = None,
) -> BenchBuilderResult:
    bench_path = bench_path or RETRIEVAL_BENCH_FILE
    auto_path = auto_path or RETRIEVAL_BENCH_AUTO_FILE
    max_total = max_total if max_total is not None else BENCH_AUTO_MAX_CASES
    per_run = per_run if per_run is not None else BENCH_AUTO_PER_RUN

    manual_cases = load_bench_cases(bench_path)
    auto_cases = load_bench_cases(auto_path)
    if len(auto_cases) >= max_total:
        return BenchBuilderResult(
            status="skipped",
            message=f"Auto bench already at cap ({len(auto_cases)}/{max_total}).",
        )

    covered = _covered_titles(manual_cases) | _covered_titles(auto_cases)
    candidates = {
        title: thesis
        for title, thesis in _thesis_by_title(rag.get_facet_entries()).items()
        if title not in covered
    }
    if not candidates:
        return BenchBuilderResult(
            status="skipped", message="No uncovered documents with facets."
        )

    added: list[dict] = []
    rejected = 0
    budget = min(per_run, max_total - len(auto_cases))
    for title, thesis in candidates.items():
        if len(added) >= budget:
            break
        question = llm.generate_bench_question(title, thesis)
        if not isinstance(question, str) or not question:
            continue

        # Quality gate: only lock in capability the system has TODAY.
        try:
            results = rag.query_notes(question, top_k=5, hybrid=True)
        except Exception as e:
            logging.warning(f"Bench builder gate query failed for {title}: {e}")
            continue
        if not any(_matches_expected(item, title) for item in results):
            rejected += 1
            logging.info(f"Bench builder: candidate rejected by gate: {title}")
            continue

        added.append({
            "query": question,
            "expected_top_k": [title],
            "top_k": 5,
            "hybrid": True,
            "auto_generated": True,
            "added": datetime.now().strftime("%Y-%m-%d"),
        })

    if added:
        _write_auto_cases(auto_path, auto_cases + added)

    return BenchBuilderResult(
        status="succeeded" if added else "skipped",
        message=(
            f"Bench builder: +{len(added)} auto case(s), {rejected} rejected by gate, "
            f"{len(auto_cases) + len(added)}/{max_total} total."
        ),
        added=added,
        rejected=rejected,
    )


def _write_auto_cases(auto_path: Path, cases: list[dict]) -> None:
    auto_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        "# Auto-generated regression queries (bench builder).\n"
        "# Each case passed a quality gate at creation time: the system answered it\n"
        "# correctly then. A future failure = retrieval regression. Safe to delete.\n"
        + yaml.safe_dump({"queries": cases}, allow_unicode=True, sort_keys=False)
    )
    tmp = auto_path.with_name(auto_path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(auto_path)
