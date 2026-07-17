"""Phase 3: dual-strength decay model, hysteresis, nightly pass,
revival-rate calibration, and the simulation harness."""

import json
from datetime import datetime, timedelta
from pathlib import Path


from maintenance.cortex_decay_pass import run_decay_pass
from maintenance.decay_simulation import simulate
from services.cortex_decay import (
    derive_status,
    half_life_days,
    load_params,
    reinforce,
    retrievability,
)
from services.cortex_store import CortexPage, load_all_pages, make_claim_id, save_cortex_page

NOW = datetime(2026, 6, 12, 4, 0, 0)


def _page(cortex_dir, claim, *, S=1.0, status="active", reinforced=None, **kw):
    reinforced = (reinforced or NOW).isoformat(timespec="seconds")
    page = CortexPage(
        claim_id=make_claim_id(claim),
        path=cortex_dir / f"{claim[:24]}.md",
        claim=claim,
        S=S,
        status=status,
        last_reinforced_at=reinforced,
        created=reinforced,
        updated=reinforced,
        **kw,
    )
    save_cortex_page(page)
    return page


class TestModelMath:
    def test_half_life_grows_with_S(self):
        assert half_life_days(1, base_days=21, growth=1.8) == 21 * 1.8
        assert half_life_days(3, base_days=21, growth=1.8) > half_life_days(
            2, base_days=21, growth=1.8
        )

    def test_retrievability_decays_and_fails_open(self):
        fresh = retrievability(1.0, NOW.isoformat(), base_days=21, growth=1.8, now=NOW)
        assert fresh == 1.0
        old = retrievability(
            1.0,
            (NOW - timedelta(days=200)).isoformat(),
            base_days=21,
            growth=1.8,
            now=NOW,
        )
        assert old < 0.1
        assert retrievability(1.0, "garbage-timestamp", base_days=21, growth=1.8) == 1.0

    def test_spacing_effect(self):
        page_fresh = CortexPage(
            claim_id="c1",
            path=Path("x"),
            claim="c",
            S=1.0,
            last_reinforced_at=NOW.isoformat(timespec="seconds"),
        )
        delta_fresh = reinforce(page_fresh, 1.0, params={"base_days": 21, "growth": 1.8}, now=NOW)
        assert delta_fresh < 0.01  # R≈1 → almost nothing

        page_stale = CortexPage(
            claim_id="c2",
            path=Path("x"),
            claim="c",
            S=1.0,
            last_reinforced_at=(NOW - timedelta(days=365)).isoformat(timespec="seconds"),
        )
        delta_stale = reinforce(page_stale, 1.0, params={"base_days": 21, "growth": 1.8}, now=NOW)
        assert delta_stale > 0.9  # 快被遺忘 → 大漲
        assert page_stale.last_reinforced_at == NOW.isoformat(timespec="seconds")


class TestHysteresis:
    def test_demote_promote_asymmetry(self):
        # Demote boundaries
        assert derive_status("active", 0.49) == "fading"
        assert derive_status("fading", 0.19) == "dormant"
        # The flap zone: R=0.55 keeps fading fading (promote needs >0.6)
        assert derive_status("fading", 0.55) == "fading"
        assert derive_status("fading", 0.61) == "active"
        # Dormant revival path
        assert derive_status("dormant", 0.25) == "dormant"
        assert derive_status("dormant", 0.35) == "fading"
        assert derive_status("dormant", 0.95) == "active"

    def test_falsified_is_terminal(self):
        assert derive_status("falsified", 1.0) == "falsified"


class FakeRAG:
    def __init__(self):
        self.removed = []
        self.added = []

    def remove_facets(self, path):
        self.removed.append(path.name)

    def add_facets(self, path, title, facets, tags=None):
        self.added.append(title)


class FakeLLM:
    def __init__(self, verdict="survived", hits=()):
        self.verdict = verdict
        self.refute_calls = []
        self.trace_store = self
        self.hits = set(hits)

    def recently_retrieved_titles(self, since_days=30):
        return self.hits

    def refute_insight(self, claim, sources, **kwargs):
        self.refute_calls.append(claim)
        return {"verdict": self.verdict, "notes": ""}


def _env(tmp_path):
    return dict(
        cortex_dir=tmp_path / "Cortex",
        state_file=tmp_path / "decay_state.json",
        log_path=tmp_path / "maintenance.log.md",
        pages_dir=tmp_path / "pages",
        notes_dir=tmp_path / "Notes",
        enabled=True,
        now=NOW,
    )


class TestDecayPass:
    def test_old_page_demotes_and_loses_facets(self, tmp_path):
        env = _env(tmp_path)
        page = _page(
            env["cortex_dir"],
            "Forgotten claim from long ago.",
            reinforced=NOW - timedelta(days=400),
        )
        rag = FakeRAG()

        result = run_decay_pass(FakeLLM(), rag, **env)

        assert (page.claim_id, "active", "dormant") in result.transitions
        assert rag.removed == [page.path.name]
        reloaded = load_all_pages(env["cortex_dir"])[0]
        assert reloaded.status == "dormant"
        state = json.loads(env["state_file"].read_text())
        assert state["transitions"][0]["to"] == "dormant"

    def test_retrieval_hit_revives_dormant_page(self, tmp_path):
        env = _env(tmp_path)
        page = _page(
            env["cortex_dir"],
            "Dormant but queried claim.",
            status="dormant",
            reinforced=NOW - timedelta(days=400),
        )
        rag = FakeRAG()
        llm = FakeLLM(hits=[page.claim_id])

        result = run_decay_pass(llm, rag, **env)

        # Reinforcement resets R→1 → promoted straight to active, facets back.
        assert (page.claim_id, "dormant", "active") in result.transitions
        assert rag.added == [page.claim_id]
        # GAIN_RETRIEVAL=0.5 at R≈0 → ΔS ≈ 0.5 (full spacing benefit).
        assert load_all_pages(env["cortex_dir"])[0].S > 1.45

    def test_retrieval_reinforced_once_per_day(self, tmp_path):
        env = _env(tmp_path)
        page = _page(env["cortex_dir"], "Hot claim hit twice today.")
        llm = FakeLLM(hits=[page.claim_id])
        run_decay_pass(llm, FakeRAG(), **env)
        second = run_decay_pass(llm, FakeRAG(), **env)
        assert second.reinforced == 0

    def test_user_edit_detected_via_mtime_not_updated(self, tmp_path):
        env = _env(tmp_path)
        page = _page(env["cortex_dir"], "Claim the user will touch.")
        run_decay_pass(FakeLLM(), FakeRAG(), **env)  # baseline recorded

        # Simulate an Obsidian edit: mtime moves, frontmatter `updated` doesn't.
        import os
        import time

        os.utime(page.path, (time.time() + 99, time.time() + 99))

        result = run_decay_pass(FakeLLM(), FakeRAG(), **env)
        assert result.reinforced == 1

    def test_revalidation_quota_and_outcomes(self, tmp_path):
        env = _env(tmp_path)
        (env["pages_dir"]).mkdir(parents=True)
        (env["pages_dir"] / "Src.md").write_text("evidence body " * 30, encoding="utf-8")
        for i in range(4):
            _page(
                env["cortex_dir"],
                f"Fading claim number {i} here.",
                status="fading",
                S=4.0 - i,
                reinforced=NOW - timedelta(days=40),
                evidence=[
                    {"insight": "i.md", "sources": ["Src"], "date": "2026-06-01", "summary": "s"}
                ],
            )
        llm = FakeLLM(verdict="survived")

        result = run_decay_pass(llm, FakeRAG(), **env, revalidations=3)

        assert result.revalidated == 3  # quota
        assert len(llm.refute_calls) == 3  # 高 S 優先
        assert "number 3" not in " ".join(llm.refute_calls)

    def test_failed_revalidation_dents_confidence(self, tmp_path):
        env = _env(tmp_path)
        (env["pages_dir"]).mkdir(parents=True)
        (env["pages_dir"] / "Src.md").write_text("evidence body", encoding="utf-8")
        _page(
            env["cortex_dir"],
            "Fading claim that no longer holds.",
            status="fading",
            reinforced=NOW - timedelta(days=40),
            evidence=[
                {"insight": "i.md", "sources": ["Src"], "date": "2026-06-01", "summary": "s"}
            ],
            confidence=0.5,
        )
        result = run_decay_pass(FakeLLM(verdict="refuted"), FakeRAG(), **env)

        assert result.revalidation_failures == 1
        assert load_all_pages(env["cortex_dir"])[0].confidence == 0.4

    def test_revalidation_does_not_use_superseded_sources(self, tmp_path):
        env = _env(tmp_path)
        env["pages_dir"].mkdir(parents=True)
        (env["pages_dir"] / "Withdrawn.md").write_text("obsolete evidence", encoding="utf-8")
        _page(
            env["cortex_dir"],
            "Fading claim with only withdrawn evidence.",
            status="fading",
            reinforced=NOW - timedelta(days=40),
            evidence=[
                {
                    "insight": "i.md",
                    "sources": ["Withdrawn"],
                    "date": "2026-06-01",
                    "summary": "old",
                    "superseded_by": "new-revision",
                }
            ],
        )
        llm = FakeLLM()

        result = run_decay_pass(llm, FakeRAG(), **env)

        assert result.revalidated == 0
        assert llm.refute_calls == []

    def test_flag_off_skips(self, tmp_path):
        env = _env(tmp_path)
        env["enabled"] = False
        _page(env["cortex_dir"], "Any claim.")
        assert run_decay_pass(FakeLLM(), FakeRAG(), **env).status == "skipped"


class TestCalibration:
    def test_high_revival_rate_slows_decay(self, tmp_path):
        env = _env(tmp_path)
        _page(env["cortex_dir"], "Anchor page for the pass.")
        # Seed a transition history: 20 demotions, 5 revived (25% > 10% target).
        transitions = []
        for i in range(20):
            transitions.append(
                {
                    "claim_id": f"c{i}",
                    "from": "fading",
                    "to": "dormant",
                    "ts": "2026-05-01T00:00:00",
                }
            )
        for i in range(5):
            transitions.append(
                {
                    "claim_id": f"c{i}",
                    "from": "dormant",
                    "to": "fading",
                    "ts": "2026-05-10T00:00:00",
                }
            )
        env["state_file"].parent.mkdir(parents=True, exist_ok=True)
        env["state_file"].write_text(
            json.dumps(
                {
                    "params": {},
                    "observed": {},
                    "transitions": transitions,
                    "last_calibration": "",
                }
            ),
            encoding="utf-8",
        )

        result = run_decay_pass(FakeLLM(), FakeRAG(), **env)

        assert result.calibrated
        params = load_params(env["state_file"])
        assert params["base_days"] == round(21 * 1.2, 2)  # damped +20%

    def test_insufficient_samples_no_calibration(self, tmp_path):
        env = _env(tmp_path)
        _page(env["cortex_dir"], "Anchor page.")
        result = run_decay_pass(FakeLLM(), FakeRAG(), **env)
        assert not result.calibrated


class TestSimulation:
    def test_simulation_reflects_decay(self, tmp_path):
        cortex = tmp_path / "Cortex"
        old = _page(cortex, "Ancient single-event claim.", reinforced=NOW - timedelta(days=300))
        old.created = (NOW - timedelta(days=300)).isoformat(timespec="seconds")
        save_cortex_page(old)
        fresh = _page(cortex, "Fresh claim from yesterday.", reinforced=NOW - timedelta(days=1))
        fresh.created = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
        save_cortex_page(fresh)

        cell = simulate(load_all_pages(cortex), base_days=21, growth=1.8, now=NOW)

        assert cell.status_counts["dormant"] == 1  # the ancient one
        assert cell.status_counts["active"] == 1  # the fresh one
        assert 0 < cell.mean_r < 1
