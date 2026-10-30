"""Cortex tension scan — the READ side, surfacing dissent (Phase 5, F3).

The anti-echo-chamber counterpart to recall: instead of "what do I believe",
it answers "where is my knowledge in tension". Pure scan over Cortex pages —
no LLM, no embedding — so it's robust regardless of retrieval quality. Four
buckets, each a different failure of epistemic hygiene:

- contradictions: claim pairs the ledger flagged as conflicting (live dissent).
- dogmatic: confident but UNfalsifiable claims — the echo-chamber fuel (a
  belief that can't be wrong only ever self-reinforces).
- thin_evidence: claims standing on <=1 evidence source.
- falsified: disproven beliefs (the graveyard), shown for transparency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.config import (
    CORTEX_DIR,
    CORTEX_TENSION_DOGMATIC_FALS,
    CORTEX_TENSION_DOGMATIC_CONF,
    CORTEX_TENSION_THIN_EVIDENCE_MAX,
)
from services.cortex_store import CortexPage, active_evidence, load_all_pages


@dataclass
class TensionReport:
    contradictions: list[tuple[CortexPage, list[str]]] = field(default_factory=list)
    dogmatic: list[CortexPage] = field(default_factory=list)
    thin_evidence: list[CortexPage] = field(default_factory=list)
    falsified: list[CortexPage] = field(default_factory=list)
    total_pages: int = 0
    snapshot: dict = field(default_factory=dict)

    @property
    def any(self) -> bool:
        return bool(self.contradictions or self.dogmatic or self.thin_evidence or self.falsified)


def _evidence_count(page: CortexPage) -> int:
    return len(active_evidence(page))


def scan_tensions(cortex_dir: Path | None = None) -> TensionReport:
    """Categorize Cortex pages into tension buckets. Pure, fail-open."""
    cortex_dir = cortex_dir or CORTEX_DIR
    pages = load_all_pages(cortex_dir)
    by_id = {p.claim_id: p for p in pages}
    report = TensionReport(total_pages=len(pages))

    # All diagnostics derive from the same page snapshots as the tension buckets.
    by_status: dict[str, dict] = {}
    for page in pages:
        row = by_status.setdefault(
            page.status, {"total": 0, "thin": 0, "a2_pages": 0, "a2_evidence": 0}
        )
        evidence = active_evidence(page)
        a2 = sum(str(e.get("insight", "")).startswith("[EvidenceTrace] ") for e in evidence)
        row["total"] += 1
        row["thin"] += int(len(evidence) <= CORTEX_TENSION_THIN_EVIDENCE_MAX)
        row["a2_pages"] += int(a2 > 0)
        row["a2_evidence"] += a2
    report.snapshot = {"by_status": by_status}

    seen_pairs: set[frozenset] = set()
    for p in pages:
        if p.status == "falsified":
            report.falsified.append(p)
            continue  # a disproven claim isn't also "dogmatic/thin" — it's dead

        # Contradiction links (dedup A↔B; resolve ids to claim text).
        if p.contradictions:
            fresh = []
            for cid in p.contradictions:
                pair = frozenset((p.claim_id, cid))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                other = by_id.get(cid)
                fresh.append(other.claim.strip().replace("\n", " ") if other else cid)
            if fresh:
                report.contradictions.append((p, fresh))

        # Dogmatic: confident yet unfalsifiable — can't be wrong, so it only
        # ever self-reinforces. The echo-chamber's structural fuel.
        if (
            p.falsifiability is not None
            and p.falsifiability <= CORTEX_TENSION_DOGMATIC_FALS
            and p.confidence >= CORTEX_TENSION_DOGMATIC_CONF
        ):
            report.dogmatic.append(p)

        # Thin evidence: standing on too few sources.
        if _evidence_count(p) <= CORTEX_TENSION_THIN_EVIDENCE_MAX:
            report.thin_evidence.append(p)

    report.snapshot["dogmatic_examples"] = [
        {"claim": p.claim.strip()[:90], "status": p.status} for p in report.dogmatic[:4]
    ]
    report.snapshot["active_thin_examples"] = [
        p.claim.strip()[:90] for p in report.thin_evidence if p.status == "active"
    ][:4]
    return report
