"""One-off backfill: assess falsifiability for existing Cortex pages.

Phase 2.5 D5 — pages created before the fifth signal existed get their
falsifiability measured retroactively as the "before" baseline.
Confidence is deliberately NOT recomputed: those pages carry
reconsolidation history, and rewriting confidence would destroy it.
Only the measurement (falsifiability + falsifier) is added.

Usage: ../venv/bin/python -m maintenance.backfill_falsifiability
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.config import CORTEX_DIR
from services.cortex_store import load_all_pages, save_cortex_page


@dataclass
class FalsifiabilityBackfillResult:
    scanned: int = 0
    backfilled: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


def backfill_falsifiability(
    llm, *, cortex_dir: Path = None, force: bool = False
) -> FalsifiabilityBackfillResult:
    """force=True re-assesses pages that already have a score (used when
    the falsifier format changes, e.g. the bilingual upgrade)."""
    cortex_dir = cortex_dir or CORTEX_DIR
    result = FalsifiabilityBackfillResult()

    for page in load_all_pages(cortex_dir):
        result.scanned += 1
        if page.falsifiability is not None and not force:
            result.skipped += 1
            continue
        try:
            assessment = llm.assess_falsifiability(page.claim)
            score = assessment.get("score") if isinstance(assessment, dict) else None
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                result.failed.append(f"{page.path.name}: no usable score")
                continue
            page.falsifiability = max(0.0, min(1.0, float(score)))
            falsifier = assessment.get("falsifier")
            page.falsifier = falsifier.strip()[:420] if isinstance(falsifier, str) else ""
            # Measurement only — confidence, S, and timestamps stay untouched.
            save_cortex_page(page)
            result.backfilled += 1
            logging.info(
                f"Falsifiability backfilled: {page.claim[:40]}… → {page.falsifiability}"
            )
        except Exception as e:
            logging.exception(f"Falsifiability backfill failed for {page.path.name}")
            result.failed.append(f"{page.path.name}: {e}")
    return result


def main():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from services.llm_client import LLMClient

    result = backfill_falsifiability(LLMClient(), force="--force" in sys.argv)
    print(
        f"scanned={result.scanned} backfilled={result.backfilled} "
        f"skipped={result.skipped} failed={len(result.failed)}"
    )
    for item in result.failed:
        print("FAILED:", item)


if __name__ == "__main__":
    main()
