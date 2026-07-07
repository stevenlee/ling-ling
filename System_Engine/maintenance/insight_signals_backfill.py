"""Backfill of Phase-1 quality signals onto historical insights.

Insights generated before Phase 1 landed carry no `signals` block, so the
Phase-2 candidate gate skips them. This backfill computes signals for each
unsigned insight and writes them into the frontmatter — body bytes are
preserved untouched.

``force=True`` additionally re-signs already-signed insights. This exists
because the 2026-07-07 signal fixes (nested-pages source resolution +
embedding-dim sidecar purge) revealed that every signal written between
2026-05 and 2026-07 was wrong (bridging stuck at 0.0, novelty stuck at
null); the stored values need recomputation, not just gap-filling.

Related titles come from frontmatter keys when present, else from the mirror
filename convention `[stamp][Related Doc][command].md` (segment 2; "Vault"
means none). The filename segment joins multiple source titles with "+" and
truncates each to a fixed width, so fragments are resolved back to real page
stems via exact-then-prefix matching; unresolvable fragments are kept raw
(harmless downstream no-op) and reported in ``BackfillResult.unresolved``.

Files are processed in ``date_created`` order: novelty compares each insight
against the sidecar history accumulated so far, so replaying chronologically
reconstructs the signal the live path would have produced.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import INSIGHTS_DIR
from core.markdown_doc import MarkdownDocument
from core.vault_utils import sanitize_filename

# Directory handles are read off the insight_signals module (not core.config)
# so that title resolution here and source loading in compute_signals always
# see the same dirs — tests monkeypatch services.insight_signals.PAGES_DIR.
import services.insight_signals as _sig
from services.insight_signals import compute_signals

_MIRROR_NAME_RE = re.compile(r"^\[(?P<stamp>[^\]]*)\]\[(?P<related>[^\]]*)\]\[(?P<cmd>[^\]]*)\]")


@dataclass
class BackfillResult:
    scanned: int = 0
    backfilled: int = 0
    resigned: int = 0
    skipped_signed: int = 0
    unresolved: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _vault_stems() -> set[str]:
    stems: set[str] = set()
    for directory in (_sig.PAGES_DIR, _sig.NOTES_DIR):
        if directory.exists():
            stems.update(p.stem for p in directory.rglob("*.md"))
    return stems


def _resolve_fragment(fragment: str, stems: set[str]) -> str | None:
    """Map one (possibly truncated) filename fragment to a real page stem."""
    fragment = sanitize_filename(fragment.strip())
    if not fragment or fragment.lower() == "vault":
        return None
    if fragment in stems:
        return fragment
    candidates = [s for s in stems if s.startswith(fragment)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # A bare doc title prefix-matches all its (Part N)/(Synthesis)/(Stitched)
        # siblings — prefer the Synthesis digest when it disambiguates.
        synthesis = [c for c in candidates if c.endswith(" (Synthesis)")]
        if len(synthesis) == 1:
            return synthesis[0]
    return None


def _titles_from_mirror_segment(segment: str, stems: set[str], unresolved: list[str]) -> list[str]:
    titles: list[str] = []
    for fragment in segment.split("+"):
        resolved = _resolve_fragment(fragment, stems)
        if resolved:
            if resolved not in titles:
                titles.append(resolved)
        elif fragment.strip() and fragment.strip().lower() != "vault":
            # Keep the raw fragment: a clean single title still loads fine
            # downstream; garbage (truncated tail, "+"-in-title leftovers)
            # is a harmless no-op there.
            titles.append(fragment.strip())
            unresolved.append(fragment.strip())
    return titles


def _related_titles(path: Path, meta: dict, stems: set[str], unresolved: list[str]) -> list[str]:
    for key in ("related_docs", "related_titles", "target_titles"):
        value = meta.get(key)
        if isinstance(value, list) and value:
            return [str(v) for v in value]
    m = _MIRROR_NAME_RE.match(path.name)
    if m:
        related = m.group("related").strip()
        if related and related.lower() != "vault":
            return _titles_from_mirror_segment(related, stems, unresolved)
    return []


def _replay_key(path: Path, meta: dict) -> str:
    """Chronological sort key (digit-normalized) for novelty replay order."""
    created = meta.get("date_created")
    if created:
        digits = re.sub(r"\D", "", str(created))
        if digits:
            return digits.ljust(14, "0")[:14]
    m = _MIRROR_NAME_RE.match(path.name)
    if m and m.group("stamp"):
        digits = re.sub(r"\D", "", m.group("stamp"))
        if digits:
            return digits.ljust(14, "0")[:14]
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d%H%M%S")
    except OSError:
        return "99999999999999"


def backfill_signals(
    rag,
    llm,
    *,
    insights_dir: Path = None,
    run_refute: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> BackfillResult:
    insights_dir = insights_dir or INSIGHTS_DIR
    result = BackfillResult()
    if not insights_dir.exists():
        return result

    stems = _vault_stems()

    # Load everything up front so processing can run in chronological order
    # (novelty replay), not filename order.
    docs: list[tuple[str, Path, str, MarkdownDocument]] = []
    for path in sorted(insights_dir.glob("*.md")):
        result.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
            doc = MarkdownDocument.from_text(text, path=path)
            docs.append((_replay_key(path, doc.meta), path, text, doc))
        except Exception as e:
            logging.exception(f"Signals backfill failed to read {path.name}")
            result.failed.append(f"{path.name}: {e}")
    docs.sort(key=lambda t: t[0])

    for _, path, text, doc in docs:
        try:
            meta = doc.meta
            already_signed = isinstance(meta.get("signals"), dict)
            if already_signed and not force:
                result.skipped_signed += 1
                continue

            signals = compute_signals(
                text,
                _related_titles(path, meta, stems, result.unresolved),
                rag,
                llm,
                run_refute=run_refute,
                # dry-run must not pollute the novelty sidecar history
                update_history=not dry_run,
            )
            old_signals = meta.get("signals") if already_signed else None
            # Without a refute pass, don't clobber a verdict an earlier run earned.
            refute_verdict = signals.refute_verdict
            if refute_verdict is None and not run_refute:
                refute_verdict = (old_signals or {}).get("refute_verdict")
            meta["signals"] = {
                "groundedness": round(signals.groundedness, 4)
                if signals.groundedness is not None
                else None,
                "novelty": round(signals.novelty, 4) if signals.novelty is not None else None,
                "bridging": round(signals.bridging, 4) if signals.bridging is not None else None,
                "refute_verdict": refute_verdict,
            }
            meta["signals_version"] = 1
            meta["signals_backfilled"] = True

            if not dry_run:
                doc.save()
            if already_signed:
                result.resigned += 1
            else:
                result.backfilled += 1
            logging.info(f"Signals {'re-signed' if already_signed else 'backfilled'}: {path.name}")
        except Exception as e:
            logging.exception(f"Signals backfill failed for {path.name}")
            result.failed.append(f"{path.name}: {e}")
    return result
