"""Seed sampler: deterministic interest+exploration target selection."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from services.seed_sampler import SeedSampler


class FakeRAG:
    def __init__(self, titles):
        self.titles = set(titles)

    def get_all_indexed_titles(self):
        return self.titles


class FakeTrace:
    def __init__(self, hits):
        self.hits = set(hits)

    def recently_retrieved_titles(self, since_days=30):
        return self.hits


def _sampler(tmp_path, titles, hits=(), epsilon=0.5):
    return SeedSampler(
        FakeRAG(titles), FakeTrace(hits),
        state_file=tmp_path / "seed_history.json", epsilon=epsilon,
    )


class TestPoolFiltering:
    def test_excludes_parts_stitched_cortex_underscore(self, tmp_path):
        sampler = _sampler(tmp_path, [
            "Good Doc", "Book (Part 3)", "Book (Stitched)",
            "cortex-abc123", "_tagScrapbook", "Another Doc",
        ])
        assert sampler._candidate_titles() == ["Another Doc", "Good Doc"]

    def test_empty_pool_returns_empty(self, tmp_path):
        assert _sampler(tmp_path, []).select_targets(2) == []


class TestSelection:
    def test_exploit_prefers_interest_hits(self, tmp_path):
        sampler = _sampler(
            tmp_path, ["Cold A", "Cold B", "Hot Doc"],
            hits=["Hot Doc"], epsilon=0.0,
        )
        targets = sampler.select_targets(1)
        assert targets == ["Hot Doc"]

    def test_explore_share_picks_least_recently_sampled(self, tmp_path):
        state = tmp_path / "seed_history.json"
        state.write_text(json.dumps({
            "Hot Doc": "2026-06-10T00:00:00",
            "Stale Doc": "2026-01-01T00:00:00",
            # "Never Doc" absent = never sampled
        }), encoding="utf-8")
        sampler = SeedSampler(
            FakeRAG(["Hot Doc", "Stale Doc", "Never Doc"]),
            FakeTrace(["Hot Doc"]),
            state_file=state, epsilon=0.5,
        )
        targets = sampler.select_targets(2)
        # 1 explore slot (never-sampled wins) + 1 exploit slot (hit).
        assert targets == ["Never Doc", "Hot Doc"]

    def test_ledger_updated_after_selection(self, tmp_path):
        sampler = _sampler(tmp_path, ["Only Doc"], epsilon=0.0)
        sampler.select_targets(1)
        ledger = json.loads((tmp_path / "seed_history.json").read_text())
        assert "Only Doc" in ledger

    def test_rotation_over_runs(self, tmp_path):
        """Repeated runs rotate through the pool instead of repeating."""
        titles = ["A", "B", "C", "D"]
        seen = []
        for _ in range(4):
            sampler = _sampler(tmp_path, titles, epsilon=1.0)  # pure explore
            seen.extend(sampler.select_targets(1))
        assert sorted(seen) == titles                # everyone got a turn

    def test_corrupted_ledger_recovers(self, tmp_path):
        state = tmp_path / "seed_history.json"
        state.write_text("{broken", encoding="utf-8")
        sampler = SeedSampler(
            FakeRAG(["Doc"]), FakeTrace([]), state_file=state, epsilon=0.0,
        )
        assert sampler.select_targets(1) == ["Doc"]
