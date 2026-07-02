"""One-shot backfill of Phase-1 quality signals onto historical insights.

Insights generated before Phase 1 landed carry no `signals` block, so the
Phase-2 candidate gate skips them. This backfill computes signals for each
unsigned insight and writes them into the frontmatter — body bytes are
preserved untouched. Used to feed the accelerated Phase 1+2 validation
runs with the historical backlog.

Related titles come from the mirror filename convention
`[stamp][Related Doc][command].md` (segment 2; "Vault" means none).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.config import INSIGHTS_DIR
from core.markdown_doc import MarkdownDocument
from services.insight_signals import compute_signals

_MIRROR_NAME_RE = re.compile(r"^\[(?P<stamp>[^\]]*)\]\[(?P<related>[^\]]*)\]\[(?P<cmd>[^\]]*)\]")


@dataclass
class BackfillResult:
    scanned: int = 0
    backfilled: int = 0
    skipped_signed: int = 0
    failed: list[str] = field(default_factory=list)


def _related_titles(path: Path, meta: dict) -> list[str]:
    for key in ("related_docs", "related_titles", "target_titles"):
        value = meta.get(key)
        if isinstance(value, list) and value:
            return [str(v) for v in value]
    m = _MIRROR_NAME_RE.match(path.name)
    if m:
        related = m.group("related").strip()
        if related and related.lower() != "vault":
            return [related]
    return []


def backfill_signals(
    rag, llm, *, insights_dir: Path = None, run_refute: bool = True
) -> BackfillResult:
    insights_dir = insights_dir or INSIGHTS_DIR
    result = BackfillResult()
    if not insights_dir.exists():
        return result

    for path in sorted(insights_dir.glob("*.md")):
        result.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
            doc = MarkdownDocument.from_text(text, path=path)
            meta = doc.meta
            if isinstance(meta.get("signals"), dict):
                result.skipped_signed += 1
                continue

            signals = compute_signals(
                text, _related_titles(path, meta), rag, llm, run_refute=run_refute
            )
            meta["signals"] = {
                "groundedness": round(signals.groundedness, 4)
                if signals.groundedness is not None
                else None,
                "novelty": round(signals.novelty, 4) if signals.novelty is not None else None,
                "bridging": round(signals.bridging, 4) if signals.bridging is not None else None,
                "refute_verdict": signals.refute_verdict,
            }
            meta["signals_version"] = 1
            meta["signals_backfilled"] = True

            doc.save()
            result.backfilled += 1
            logging.info(f"Signals backfilled: {path.name}")
        except Exception as e:
            logging.exception(f"Signals backfill failed for {path.name}")
            result.failed.append(f"{path.name}: {e}")
    return result
