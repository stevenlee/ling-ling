"""Seed sampler — interest-weighted, exploration-guaranteed insight targets.

Vault-wide insight rumination produced abstract, hard-to-falsify claims
(measured: 80% broken-link rate among gate-passers, refute coverage 0%).
Doc-anchored insights are the treatment: each night the sampler picks
concrete target documents for a targeted montecarlo run.

Selection is DETERMINISTIC (no RNG — predictable and testable):

- exploit slots: highest interest weight = recent retrieval hits
  (the user is asking about it) breaking ties by least-recently-sampled;
- explore slots (ceil(n × epsilon)): least-recently-sampled overall —
  the ε-greedy guarantee that the brain doesn't only dream about its
  comfort zone.

The only persisted state is the sampling ledger (title → last sampled),
atomic writes, corruption-safe.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

from core.config import INSIGHT_SEED_EPSILON, SEED_HISTORY_FILE

_EXCLUDED_MARKERS = ("(Part ", "(Stitched)")


class SeedSampler:
    def __init__(
        self,
        rag,
        trace_store=None,
        *,
        state_file: Path = None,
        epsilon: float = None,
    ):
        self.rag = rag
        self.trace_store = trace_store
        self.state_file = state_file or SEED_HISTORY_FILE
        self.epsilon = INSIGHT_SEED_EPSILON if epsilon is None else epsilon

    # ── Candidate pool ───────────────────────────────────────────────

    def _candidate_titles(self) -> list[str]:
        try:
            titles = self.rag.get_all_indexed_titles()
        except Exception as e:
            logging.warning(f"SeedSampler: title listing failed: {e}")
            return []
        out = []
        for title in titles:
            if not isinstance(title, str) or not title.strip():
                continue
            if title.startswith("cortex-") or title.startswith("_"):
                continue
            if any(marker in title for marker in _EXCLUDED_MARKERS):
                continue
            out.append(title)
        return sorted(out)

    def _interest_hits(self) -> set[str]:
        if self.trace_store is None or not hasattr(self.trace_store, "recently_retrieved_titles"):
            return set()
        try:
            return set(self.trace_store.recently_retrieved_titles(30))
        except Exception:
            return set()

    # ── Selection ────────────────────────────────────────────────────

    def select_targets(self, n: int = 2) -> list[str]:
        """Pick n targets: explore slots first (least-recently-sampled),
        then exploit slots (interest-weighted). Updates the ledger."""
        pool = self._candidate_titles()
        if not pool:
            return []
        n = min(n, len(pool))

        ledger = self._load_ledger()
        last_sampled = {t: ledger.get(t, "") for t in pool}
        hits = self._interest_hits()

        explore_count = min(n, math.ceil(n * self.epsilon)) if self.epsilon > 0 else 0
        selected: list[str] = []

        # Explore: never-sampled first (empty string sorts first), then oldest.
        by_staleness = sorted(pool, key=lambda t: (last_sampled[t], t))
        for title in by_staleness:
            if len(selected) >= explore_count:
                break
            selected.append(title)

        # Exploit: interest first (retrieval hits), ties by staleness.
        by_interest = sorted(
            (t for t in pool if t not in selected),
            key=lambda t: (t not in hits, last_sampled[t], t),
        )
        for title in by_interest:
            if len(selected) >= n:
                break
            selected.append(title)

        stamp = datetime.now().isoformat(timespec="seconds")
        for title in selected:
            ledger[title] = stamp
        self._save_ledger(ledger)
        return selected

    # ── Ledger ───────────────────────────────────────────────────────

    def _load_ledger(self) -> dict:
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.warning(f"SeedSampler: ledger unreadable, starting fresh: {e}")
        return {}

    def _save_ledger(self, ledger: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logging.warning(f"SeedSampler: ledger write failed: {e}")
