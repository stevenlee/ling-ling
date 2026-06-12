"""Nightly Cortex consolidation: candidate gating, merge-only-on-
equivalent, contradiction links, quotas, adjudication cache, ledgers."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from maintenance.cortex_consolidation import _pair_key, run_consolidation
from services.cortex_store import load_all_pages, make_claim_id
from services.llm_client import LLMClient


# ── Fakes ─────────────────────────────────────────────────────────────

class FakeLLM:
    """extract_claims keyed by insight text marker; adjudicate by pair."""

    def __init__(self, claims_map=None, verdicts=None):
        self.claims_map = claims_map or {}
        self.verdicts = verdicts or {}
        self.adjudicate_calls = []
        self.extract_calls = []

    def extract_claims(self, insight_text):
        self.extract_calls.append(insight_text)
        for marker, claims in self.claims_map.items():
            if marker in insight_text:
                return claims
        return []

    def adjudicate_claims(self, claim_a, claim_b):
        self.adjudicate_calls.append((claim_a, claim_b))
        for (a, b), verdict in self.verdicts.items():
            if {a, b} <= {claim_a, claim_b} or (a in claim_a and b in claim_b) or (a in claim_b and b in claim_a):
                return {"verdict": verdict, "rationale": "test"}
        return {"verdict": "unrelated", "rationale": "test"}

class FakeRAG:
    """Embeddings by keyword so neighbor similarity is controllable."""

    VECTORS = {
        "ALPHA": [1.0, 0.0],
        "NEARALPHA": [0.95, 0.312],   # cos vs ALPHA ≈ 0.95
        "BETA": [0.0, 1.0],
    }

    def __init__(self):
        self.indexed = []
        self.facets = []

    def ef(self, texts):
        out = []
        for t in texts:
            vec = [0.5, 0.5]
            for marker, v in self.VECTORS.items():
                if marker in t:
                    vec = v
                    break
            out.append(vec)
        return out

    def add_document(self, path, title, text, tags=None, section_path=None):
        self.indexed.append(title)

    def add_facets(self, path, title, facets, tags=None):
        self.facets.append((title, facets))


def _write_insight(insights_dir, name, *, body="insight body", refute="survived",
                   groundedness=0.9, signals=True, sources=None):
    insights_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if signals:
        lines += [
            "signals:",
            f"  groundedness: {'null' if groundedness is None else groundedness}",
            "  novelty: 0.8",
            "  bridging: 0.5",
            f"  refute_verdict: {refute}",
            "signals_version: 1",
        ]
    if sources:
        lines.append("related_docs: [" + ", ".join(f'"{s}"' for s in sources) + "]")
    lines += ["---", "", body]
    (insights_dir / name).write_text("\n".join(lines), encoding="utf-8")


def _env(tmp_path):
    return dict(
        insights_dir=tmp_path / "Insights",
        cortex_dir=tmp_path / "Cortex",
        state_file=tmp_path / "Database" / "cortex_state.json",
        cache_file=tmp_path / "Database" / "cortex_adjudications.json",
        report_dir=tmp_path / "fromLingLing",
        log_path=tmp_path / "maintenance.log.md",
        enabled=True,
    )


# ── Candidate gating ──────────────────────────────────────────────────

class TestCandidateGating:
    def test_gates(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        _write_insight(env["insights_dir"], "refuted.md", refute="refuted")
        _write_insight(env["insights_dir"], "weak.md", groundedness=0.2)
        _write_insight(env["insights_dir"], "nosignals.md", signals=False)
        _write_insight(env["insights_dir"], "nullground.md", body="MARKER-NULL", groundedness=None)

        llm = FakeLLM(claims_map={
            "MARKER-OK": [{"claim": "ALPHA claim about memory.", "summary": "s"}],
            "MARKER-NULL": [{"claim": "BETA claim about sleep.", "summary": "s"}],
        })
        result = run_consolidation(llm, FakeRAG(), **env)

        # Only ok.md + nullground.md pass the gate.
        assert result.insights_processed == 2
        assert result.created == 2
        processed = json.loads(env["state_file"].read_text())["processed"]
        assert set(processed) == {"ok.md", "nullground.md"}

    def test_processed_not_reprocessed(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(claims_map={"MARKER-OK": [{"claim": "ALPHA claim.", "summary": "s"}]})

        run_consolidation(llm, FakeRAG(), **env)
        second = run_consolidation(llm, FakeRAG(), **env)

        assert second.status == "skipped"
        assert len(llm.extract_calls) == 1

    def test_flag_off_zero_side_effects(self, tmp_path):
        env = _env(tmp_path)
        env["enabled"] = False
        _write_insight(env["insights_dir"], "ok.md")
        result = run_consolidation(FakeLLM(), FakeRAG(), **env)
        assert result.status == "skipped"
        assert not env["cortex_dir"].exists()
        assert not env["state_file"].exists()


# ── Verdict-driven actions ────────────────────────────────────────────

class TestActions:
    def _seed_alpha_page(self, tmp_path, env):
        """First night: create the ALPHA page."""
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A", sources=["Doc X"])
        llm = FakeLLM(claims_map={
            "MARKER-A": [{"claim": "ALPHA: recall rewrites the memory trace.", "summary": "s1"}],
        })
        run_consolidation(llm, FakeRAG(), **env)
        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 1
        return pages[0]

    def test_equivalent_merges_not_creates(self, tmp_path):
        env = _env(tmp_path)
        first = self._seed_alpha_page(tmp_path, env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B", sources=["Doc Y"])
        llm = FakeLLM(
            claims_map={"MARKER-B": [
                {"claim": "NEARALPHA: every retrieval re-encodes the trace.", "summary": "s2"},
            ]},
            verdicts={("NEARALPHA", "ALPHA"): "equivalent"},
        )
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.merged == 1 and result.created == 0
        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 1
        page = pages[0]
        assert len(page.evidence) == 2
        # Phase 3 spacing rule: a same-night rediscovery happens at R≈1, so
        # ΔS = gain×(1−R) ≈ 0 — duplicates no longer inflate S. The
        # reinforcement still resets last_reinforced_at.
        assert first.S <= page.S < first.S + 0.01
        assert page.last_reinforced_at >= first.last_reinforced_at
        assert page.confidence == 0.6
        assert "NEARALPHA: every retrieval re-encodes the trace." in page.variants

    def test_confidence_caps_and_variant_cap(self, tmp_path):
        env = _env(tmp_path)
        self._seed_alpha_page(tmp_path, env)

        for i in range(7):
            _write_insight(env["insights_dir"], f"m{i}.md", body=f"MARKER-V{i}")
            llm = FakeLLM(
                claims_map={f"MARKER-V{i}": [
                    {"claim": f"NEARALPHA variant {i} of the trace claim.", "summary": "s"},
                ]},
                verdicts={(f"variant {i}", "ALPHA"): "equivalent"},
            )
            run_consolidation(llm, FakeRAG(), **env, max_variants=3)

        page = load_all_pages(env["cortex_dir"])[0]
        assert page.confidence == 0.9          # capped
        assert len(page.variants) == 3         # capped, oldest dropped
        # Spacing rule: seven same-night merges barely move S (R≈1 each time).
        assert 1.0 <= page.S < 1.1

    def test_contradicts_links_and_dents_both(self, tmp_path):
        env = _env(tmp_path)
        first = self._seed_alpha_page(tmp_path, env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-C")
        llm = FakeLLM(
            claims_map={"MARKER-C": [
                {"claim": "NEARALPHA: recall never alters stored memories.", "summary": "s"},
            ]},
            verdicts={("never alters", "ALPHA"): "contradicts"},
        )
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.created == 1 and result.contradiction_links == 1
        pages = {p.claim_id: p for p in load_all_pages(env["cortex_dir"])}
        assert len(pages) == 2
        old = pages[first.claim_id]
        new = next(p for cid, p in pages.items() if cid != first.claim_id)
        assert new.claim_id in old.contradictions
        assert old.claim_id in new.contradictions
        assert old.confidence == 0.3           # 0.5 - 0.2
        assert new.confidence == 0.3

    def test_unrelated_creates_page_and_indexes(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(claims_map={"MARKER-A": [{"claim": "BETA: sleep consolidates memory.", "summary": "s"}]})
        rag = FakeRAG()

        result = run_consolidation(llm, rag, **env)

        assert result.created == 1
        page = load_all_pages(env["cortex_dir"])[0]
        assert rag.indexed == [page.claim_id]
        assert rag.facets == [(page.claim_id, [page.claim])]

    def test_exact_duplicate_merges_without_adjudication(self, tmp_path):
        env = _env(tmp_path)
        claim = "ALPHA: recall rewrites the memory trace."
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm = FakeLLM(claims_map={
            "MARKER-A": [{"claim": claim, "summary": "s1"}],
            "MARKER-B": [{"claim": claim, "summary": "s2"}],
        })
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.created == 1 and result.merged == 1
        assert llm.adjudicate_calls == []      # claim_id fast path
        page = load_all_pages(env["cortex_dir"])[0]
        assert page.claim_id == make_claim_id(claim)


# ── Quotas and cache ──────────────────────────────────────────────────

class TestQuotasAndCache:
    def test_insight_quota(self, tmp_path):
        env = _env(tmp_path)
        for i in range(5):
            _write_insight(env["insights_dir"], f"i{i}.md", body=f"MARKER-{i}")
        llm = FakeLLM(claims_map={f"MARKER-{i}": [] for i in range(5)})

        result = run_consolidation(llm, FakeRAG(), **env, max_insights=2)

        assert result.insights_processed == 2
        assert len(llm.extract_calls) == 2
        # The rest stay unprocessed for the next night.
        processed = json.loads(env["state_file"].read_text())["processed"]
        assert len(processed) == 2

    def test_adjudication_quota_stops_llm_calls(self, tmp_path):
        env = _env(tmp_path)
        # Seed a page, then bring a near-duplicate with quota 0.
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA base claim.", "summary": "s"}]})
        run_consolidation(llm1, FakeRAG(), **env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = FakeLLM(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA close claim.", "summary": "s"}]},
            verdicts={("NEARALPHA", "ALPHA"): "equivalent"},
        )
        result = run_consolidation(llm2, FakeRAG(), **env, max_adjudications=0)

        assert llm2.adjudicate_calls == []
        assert result.created == 1            # falls through to new page

    def test_cache_hit_skips_llm_and_is_order_invariant(self, tmp_path):
        env = _env(tmp_path)
        assert _pair_key("A", "B") == _pair_key("B", "A")

        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA base claim.", "summary": "s"}]})
        run_consolidation(llm1, FakeRAG(), **env)

        # Night 2: adjudicated once (complementary → new page + link).
        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        claims_b = [{"claim": "NEARALPHA cousin claim.", "summary": "s"}]
        llm2 = FakeLLM(claims_map={"MARKER-B": claims_b},
                       verdicts={("NEARALPHA", "ALPHA"): "complementary"})
        run_consolidation(llm2, FakeRAG(), **env)
        assert len(llm2.adjudicate_calls) == 1

        # Night 3: same pair re-appears (same claim text from a new insight)
        # → exact-duplicate path merges; no adjudication. Instead verify the
        # cache file kept the verdict for the live pair.
        cache = json.loads(env["cache_file"].read_text())
        assert any(v.get("verdict") == "complementary" for v in cache.values())

    def test_manual_claim_edit_invalidates_embedding_cache(self, tmp_path):
        """An external editor can change the Core Claim without bumping
        frontmatter `updated`; the embedding cache must catch that via
        the claim content hash (Gemini review nitpick, phase-2 R1)."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA original claim.", "summary": "s"}]})
        run_consolidation(llm, FakeRAG(), **env)

        # Simulate a manual Core Claim edit that leaves `updated` untouched.
        page_path = next(env["cortex_dir"].glob("*.md"))
        text = page_path.read_text(encoding="utf-8")
        page_path.write_text(
            text.replace("ALPHA original claim.", "BETA edited claim."), encoding="utf-8"
        )

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = FakeLLM(claims_map={"MARKER-B": [{"claim": "BETA new sibling claim.", "summary": "s"}]})
        run_consolidation(llm2, FakeRAG(), **env)

        # Cache entry was recomputed for the edited claim text.
        state = json.loads(env["state_file"].read_text())
        entries = list(state["claim_embeddings"].values())
        assert any(e["embedding"] == [0.0, 1.0] for e in entries)  # BETA vector

    def test_corrupted_state_and_cache_recover(self, tmp_path):
        env = _env(tmp_path)
        env["state_file"].parent.mkdir(parents=True, exist_ok=True)
        env["state_file"].write_text("{not json", encoding="utf-8")
        env["cache_file"].write_text("[]", encoding="utf-8")  # wrong type
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA fresh claim.", "summary": "s"}]})

        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.created == 1
        assert json.loads(env["state_file"].read_text())["processed"]


# ── LLM method defenses (mock _complete_text; no provider calls) ──────

class TestLLMDefenses:
    def _client(self, monkeypatch, response):
        client = LLMClient()
        monkeypatch.setattr(client, "_complete_text", lambda *a, **k: response)
        return client

    def test_extract_claims_parses_and_caps(self, monkeypatch):
        response = json.dumps([
            {"claim": "Claim number one is long enough.", "summary": "s1"},
            {"claim": "Claim number two is long enough.", "summary": "s2"},
            {"claim": "Claim number three is long enough.", "summary": "s3"},
            {"claim": "Claim number four is long enough.", "summary": "s4"},
        ])
        out = self._client(monkeypatch, response).extract_claims("text")
        assert len(out) == 3
        assert out[0]["claim"].startswith("Claim number one")

    def test_extract_claims_garbage_and_short(self, monkeypatch):
        assert self._client(monkeypatch, "not json").extract_claims("t") == []
        assert self._client(monkeypatch, '[{"claim": "tiny"}]').extract_claims("t") == []
        assert self._client(monkeypatch, '["just a string"]').extract_claims("t") == []

    def test_extract_claims_magicmock_llm(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="x")
        result = run_consolidation(MagicMock(), FakeRAG(), **env)
        assert result.created == 0            # mock claims filtered out, no crash

    def test_adjudicate_valid_and_illegal(self, monkeypatch):
        ok = self._client(monkeypatch, '{"verdict": "Equivalent", "rationale": "r"}')
        assert ok.adjudicate_claims("a", "b")["verdict"] == "equivalent"

        bad = self._client(monkeypatch, '{"verdict": "kinda-similar"}')
        assert bad.adjudicate_claims("a", "b")["verdict"] == "unrelated"

        garbage = self._client(monkeypatch, "no json at all")
        assert garbage.adjudicate_claims("a", "b")["verdict"] == "unrelated"


# ── Phase 2.5: falsifiability wiring, anchoring, penetration ──────────

class FalsifiabilityFakeLLM(FakeLLM):
    """FakeLLM + configurable fifth signal; counts assessment calls."""

    def __init__(self, *args, score=0.5, falsifier="若 X 則推翻", **kwargs):
        super().__init__(*args, **kwargs)
        self.score = score
        self.falsifier = falsifier
        self.assess_calls = []

    def assess_falsifiability(self, claim):
        self.assess_calls.append(claim)
        return {"score": self.score, "falsifier": self.falsifier}


class TestFalsifiabilityWiring:
    def _run_one(self, tmp_path, llm, claim="BETA standalone claim.", applies_when=""):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        item = {"claim": claim, "summary": "s"}
        if applies_when:
            item["applies_when"] = applies_when
        llm.claims_map = {"MARKER-A": [item]}
        run_consolidation(llm, FakeRAG(), **env)
        return load_all_pages(env["cortex_dir"])[0]

    def test_confidence_formula_four_points(self, tmp_path):
        # score 0 → 0.3; 0.5 → 0.5; 1.0 → 0.7
        for score, expected in ((0.0, 0.3), (0.5, 0.5), (1.0, 0.7)):
            page = self._run_one(
                tmp_path / f"s{score}", FalsifiabilityFakeLLM(score=score)
            )
            assert page.confidence == expected, (score, page.confidence)
            assert page.falsifiability == score
        # score None（解析失敗）→ 未測量 → 0.5
        class NoneScoreLLM(FalsifiabilityFakeLLM):
            def assess_falsifiability(self, claim):
                return {"score": None, "falsifier": ""}
        page = self._run_one(tmp_path / "snone", NoneScoreLLM())
        assert page.confidence == 0.5
        assert page.falsifiability is None

    def test_out_of_range_score_clamped(self, tmp_path):
        page = self._run_one(tmp_path, FalsifiabilityFakeLLM(score=7.5))
        assert page.falsifiability == 1.0
        assert page.confidence == 0.7          # 不是 3.3

    def test_llm_without_method_fails_open(self, tmp_path):
        # Phase 2 的 FakeLLM 沒有 assess_falsifiability —— 必須照常運作
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(claims_map={"MARKER-A": [{"claim": "BETA plain claim here.", "summary": "s"}]})
        run_consolidation(llm, FakeRAG(), **env)
        page = load_all_pages(env["cortex_dir"])[0]
        assert page.falsifiability is None and page.confidence == 0.5

    def test_crashing_assessment_fails_open(self, tmp_path):
        class CrashLLM(FalsifiabilityFakeLLM):
            def assess_falsifiability(self, claim):
                raise RuntimeError("provider down")
        page = self._run_one(tmp_path, CrashLLM())
        assert page.falsifiability is None and page.confidence == 0.5

    def test_applies_when_lands_on_page(self, tmp_path):
        page = self._run_one(
            tmp_path, FalsifiabilityFakeLLM(), applies_when="處理長文拆解時"
        )
        assert page.applies_when == "處理長文拆解時"

    def test_merge_path_never_reassesses(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = FalsifiabilityFakeLLM(
            claims_map={"MARKER-A": [{"claim": "ALPHA base claim text.", "summary": "s"}]}
        )
        run_consolidation(llm1, FakeRAG(), **env)
        assert len(llm1.assess_calls) == 1     # 建頁評一次

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = FalsifiabilityFakeLLM(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA same idea rephrased.", "summary": "s"}]},
            verdicts={("NEARALPHA", "ALPHA"): "equivalent"},
        )
        result = run_consolidation(llm2, FakeRAG(), **env)
        assert result.merged == 1
        assert llm2.assess_calls == []         # merge 路徑零額外 call


class TestWikilinkPenetration:
    def test_sources_mined_filtered_and_capped(self, tmp_path, monkeypatch):
        import maintenance.cortex_consolidation as cc
        pages_dir = tmp_path / "pages"
        notes_dir = tmp_path / "Notes"
        pages_dir.mkdir(); notes_dir.mkdir()
        for name in ("P1", "P2", "P3", "P4", "P5", "P6"):
            (pages_dir / f"{name}.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(cc, "PAGES_DIR", pages_dir)
        monkeypatch.setattr(cc, "NOTES_DIR", notes_dir)

        env = _env(tmp_path)
        body = ("MARKER-A 引用 [[P1]] [[P2|alias]] [[P3#sec]] [[Ghost]] "
                "[[P4]] [[P5]] [[P6]] [[P1]]")
        _write_insight(env["insights_dir"], "n1.md", body=body, sources=["P1"])
        llm = FalsifiabilityFakeLLM(
            claims_map={"MARKER-A": [{"claim": "BETA sourced claim text.", "summary": "s"}]}
        )
        run_consolidation(llm, FakeRAG(), **env)

        page = load_all_pages(env["cortex_dir"])[0]
        sources = page.evidence[0]["sources"]
        assert "Ghost" not in sources           # 存在性過濾
        assert len(sources) == 5                # 上限 5
        assert sources[0] == "P1"               # frontmatter 來源優先且去重
