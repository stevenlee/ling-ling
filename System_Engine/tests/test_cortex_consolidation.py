"""Nightly Cortex consolidation: candidate gating, merge-only-on-
equivalent, contradiction links, quotas, adjudication cache, ledgers."""

import json
from unittest.mock import MagicMock


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
            if (
                {a, b} <= {claim_a, claim_b}
                or (a in claim_a and b in claim_b)
                or (a in claim_b and b in claim_a)
            ):
                return {"verdict": verdict, "rationale": "test"}
        return {"verdict": "unrelated", "rationale": "test"}


class FakeRAG:
    """Embeddings by keyword so neighbor similarity is controllable."""

    VECTORS = {
        "ALPHA": [1.0, 0.0],
        "NEARALPHA": [0.95, 0.312],  # cos vs ALPHA ≈ 0.95 (clears both floors)
        "GAMMA": [0.68, 0.73],  # cos vs ALPHA ≈ 0.68 (links at 0.60, no merge at 0.80)
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


class MutatingRAG(FakeRAG):
    """Fires a callback once, on the first embedding call — which lands after
    the candidate scan but before the processing loop — to simulate an insight
    file regenerated (or vanishing) mid-run."""

    def __init__(self, mutate):
        super().__init__()
        self._mutate = mutate

    def ef(self, texts):
        if self._mutate is not None:
            self._mutate, mutate = None, self._mutate
            mutate()
        return super().ef(texts)


def _write_insight(
    insights_dir,
    name,
    *,
    body="insight body",
    refute="survived",
    groundedness=0.9,
    signals=True,
    sources=None,
):
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

        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [{"claim": "ALPHA claim about memory.", "summary": "s"}],
                "MARKER-NULL": [{"claim": "BETA claim about sleep.", "summary": "s"}],
            }
        )
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


# ── Processed ledger: content addressing + legacy migration ──────────


class TestProcessedLedgerContentHash:
    def _llm(self):
        return FakeLLM(
            claims_map={
                "MARKER-OK": [{"claim": "ALPHA claim about memory.", "summary": "s"}],
                "MARKER-NEW": [{"claim": "BETA claim about sleep.", "summary": "s"}],
            }
        )

    def test_same_name_content_change_is_reprocessed(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()
        run_consolidation(llm, FakeRAG(), **env)
        assert len(llm.extract_calls) == 1

        # Same filename, regenerated content → owed again.
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-NEW")
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.status == "succeeded"
        assert result.insights_processed == 1
        assert len(llm.extract_calls) == 2
        assert "MARKER-NEW" in llm.extract_calls[1]
        # The ledger now holds the new content's hash → next run is a no-op.
        third = run_consolidation(llm, FakeRAG(), **env)
        assert third.status == "skipped"
        assert len(llm.extract_calls) == 2

    def test_legacy_entry_is_stamped_not_reprocessed(self, tmp_path):
        """Pre-hash {"date", "claims"} entries must not crash, must not be
        re-billed, and must become content-addressed (lazy migration) so
        edits made AFTER the stamp are detected."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()
        run_consolidation(llm, FakeRAG(), **env)

        # Rewind the ledger entry to the legacy shape.
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        state["processed"]["ok.md"] = {"date": "2026-01-01T00:00:00", "claims": 1}
        env["state_file"].write_text(json.dumps(state), encoding="utf-8")

        result = run_consolidation(llm, FakeRAG(), **env)
        assert result.status == "skipped"  # unchanged file: no reprocess
        assert len(llm.extract_calls) == 1
        # The stamp was persisted even though the run was a no-op.
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert state["processed"]["ok.md"]["content_hash"]
        assert state["processed"]["ok.md"]["claims"] == 1  # legacy fields kept

        # From now on a content change is owed again.
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-NEW")
        result = run_consolidation(llm, FakeRAG(), **env)
        assert result.insights_processed == 1
        assert len(llm.extract_calls) == 2

    def test_corrupt_ledger_entry_recovers_by_reprocessing(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()
        run_consolidation(llm, FakeRAG(), **env)

        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        state["processed"]["ok.md"] = "not-a-dict"
        env["state_file"].write_text(json.dumps(state), encoding="utf-8")

        result = run_consolidation(llm, FakeRAG(), **env)
        assert result.status == "succeeded"  # no crash
        assert len(llm.extract_calls) == 2
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert state["processed"]["ok.md"]["content_hash"]

    def test_has_pending_insights_detects_content_change(self, tmp_path):
        from maintenance.cortex_consolidation import has_pending_insights

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()
        run_consolidation(llm, FakeRAG(), **env)
        assert has_pending_insights(env["insights_dir"], env["state_file"]) is False

        _write_insight(env["insights_dir"], "ok.md", body="MARKER-NEW")
        assert has_pending_insights(env["insights_dir"], env["state_file"]) is True

    def test_has_pending_insights_legacy_entry_is_readonly_and_covered(self, tmp_path):
        """The predicate treats a legacy entry as processed (same "owed"
        definition as run_consolidation) and never writes the state file."""
        from maintenance.cortex_consolidation import has_pending_insights

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        env["state_file"].parent.mkdir(parents=True, exist_ok=True)
        legacy = json.dumps({"processed": {"ok.md": {"date": "2026-01-01T00:00:00", "claims": 1}}})
        env["state_file"].write_text(legacy, encoding="utf-8")

        assert has_pending_insights(env["insights_dir"], env["state_file"]) is False
        assert env["state_file"].read_text(encoding="utf-8") == legacy

    def test_corrupt_processed_container_is_pending_and_readonly(self, tmp_path):
        """review P2: a non-dict processed container (list/string) must mean
        "everything owed" for BOTH callers, and the predicate stays read-only."""
        from maintenance.cortex_consolidation import has_pending_insights

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        env["state_file"].parent.mkdir(parents=True, exist_ok=True)
        for corrupt in ('{"processed": []}', '{"processed": "junk"}'):
            env["state_file"].write_text(corrupt, encoding="utf-8")
            assert has_pending_insights(env["insights_dir"], env["state_file"]) is True
            assert env["state_file"].read_text(encoding="utf-8") == corrupt

    def test_corrupt_processed_container_run_recovers(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        env["state_file"].parent.mkdir(parents=True, exist_ok=True)
        env["state_file"].write_text('{"processed": []}', encoding="utf-8")
        llm = self._llm()

        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.insights_processed == 1
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert state["processed"]["ok.md"]["content_hash"]

    def test_regenerated_as_refuted_between_scan_and_processing(self, tmp_path):
        """review P1 race: gate/meta/hash must come from the processing-time
        snapshot. A file regenerated as refuted after the scan must be neither
        consumed nor stamped processed."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()

        def regenerate_refuted():
            _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK", refute="refuted")

        result = run_consolidation(llm, MutatingRAG(regenerate_refuted), **env)

        assert llm.extract_calls == []  # not consumed
        assert result.insights_processed == 0
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert "ok.md" not in state["processed"]  # not stamped either

        # Regenerated back to healthy → owed again and processed normally.
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        result = run_consolidation(llm, FakeRAG(), **env)
        assert result.insights_processed == 1
        assert len(llm.extract_calls) == 1

    def test_processing_time_read_failure_stays_owed(self, tmp_path):
        """review P1: a pre-commit-point I/O failure must not be committed as
        processed — the unchanged file stays owed and is retried next run."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = self._llm()
        insight_path = env["insights_dir"] / "ok.md"

        result = run_consolidation(llm, MutatingRAG(insight_path.unlink), **env)

        assert llm.extract_calls == []
        assert result.insights_processed == 0
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert "ok.md" not in state["processed"]  # ledger not committed

        # The same content reappears → still owed, processed next run.
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        result = run_consolidation(llm, FakeRAG(), **env)
        assert result.insights_processed == 1
        assert len(llm.extract_calls) == 1


# ── Revision semantics: same insight ≠ independent rediscovery ────────


class TestRevisionSemantics:
    def test_revision_with_same_claim_is_not_a_rediscovery(self, tmp_path):
        """review P1: a regenerated insight re-extracting the SAME claim must
        refresh its evidence in place — no duplicate evidence, no confidence
        boost, no spacing reinforcement."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [{"claim": "ALPHA stable claim.", "summary": "v1"}],
                "MARKER-NEW": [{"claim": "ALPHA stable claim.", "summary": "v2"}],
            }
        )
        run_consolidation(llm, FakeRAG(), **env)
        page = load_all_pages(env["cortex_dir"])[0]
        assert len(page.evidence) == 1 and page.confidence == 0.5 and page.S == 1

        _write_insight(env["insights_dir"], "ok.md", body="MARKER-NEW")
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.insights_processed == 1
        assert result.merged == 0 and result.revised == 1
        page = load_all_pages(env["cortex_dir"])[0]
        assert len(page.evidence) == 1  # refreshed in place, not appended
        assert page.confidence == 0.5  # no re-boost
        assert page.S == 1  # no spacing reinforcement
        assert page.evidence[0]["summary"] == "v2"
        assert "superseded_by" not in page.evidence[0]  # still asserted

    def test_revision_dropping_a_claim_supersedes_old_evidence(self, tmp_path):
        """review P1 reconciliation: when the new revision no longer asserts a
        claim, the stale evidence entry is marked superseded_by the new
        revision instead of silently lingering as live support."""
        from maintenance.cortex_consolidation import _insight_hash

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [{"claim": "ALPHA claim about memory.", "summary": "s"}],
                "MARKER-NEW": [{"claim": "BETA claim about sleep.", "summary": "s"}],
            }
        )
        run_consolidation(llm, FakeRAG(), **env)

        _write_insight(env["insights_dir"], "ok.md", body="MARKER-NEW")
        run_consolidation(llm, FakeRAG(), **env)

        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 2
        alpha = next(p for p in pages if "ALPHA" in p.claim)
        beta = next(p for p in pages if "BETA" in p.claim)
        new_hash = _insight_hash((env["insights_dir"] / "ok.md").read_text(encoding="utf-8"))
        assert alpha.evidence[0]["superseded_by"] == new_hash  # reconciled
        assert beta.evidence[0]["revision"] == new_hash
        assert "superseded_by" not in beta.evidence[0]

    def test_valid_empty_revision_supersedes_every_old_claim(self, tmp_path):
        """A successful [] is a semantic retraction, not an LLM failure."""
        from maintenance.cortex_consolidation import _insight_hash

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [{"claim": "ALPHA claim about memory.", "summary": "v1"}],
                "MARKER-EMPTY": [],
            }
        )
        run_consolidation(llm, FakeRAG(), **env)

        _write_insight(env["insights_dir"], "ok.md", body="MARKER-EMPTY")
        result = run_consolidation(llm, FakeRAG(), **env)

        revision = _insight_hash((env["insights_dir"] / "ok.md").read_text(encoding="utf-8"))
        page = load_all_pages(env["cortex_dir"])[0]
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert result.insights_processed == 1
        assert page.evidence[0]["superseded_by"] == revision
        assert state["processed"]["ok.md"]["content_hash"] == revision

    def test_extraction_failure_stays_owed_without_retracting(self, tmp_path):
        class FailingExtractionLLM(FakeLLM):
            def extract_claims_result(self, insight_text):
                self.extract_calls.append(insight_text)
                return {"valid": False, "claims": []}

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FailingExtractionLLM()

        result = run_consolidation(llm, FakeRAG(), **env)

        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert result.insights_processed == 0
        assert "ok.md" not in state["processed"]
        assert not env["cortex_dir"].exists() or load_all_pages(env["cortex_dir"]) == []

    def test_partial_claim_failure_does_not_commit_and_retry_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        """A crash after claim 1 keeps the revision owed; retrying must not
        duplicate or reinforce the claim that was already persisted."""
        import maintenance.cortex_consolidation as consolidation

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [
                    {"claim": "ALPHA stable claim.", "summary": "first"},
                    {"claim": "BETA stable claim.", "summary": "second"},
                ]
            }
        )
        original = consolidation._Consolidator.process_claim
        calls = 0

        def fail_second_claim(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated mid-revision failure")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(consolidation._Consolidator, "process_claim", fail_second_claim)
        first = run_consolidation(llm, FakeRAG(), **env)
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert first.insights_processed == 0
        assert "ok.md" not in state["processed"]

        second = run_consolidation(llm, FakeRAG(), **env)

        assert second.insights_processed == 1
        pages = load_all_pages(env["cortex_dir"])
        assert {page.claim for page in pages} == {"ALPHA stable claim.", "BETA stable claim."}
        alpha = next(page for page in pages if page.claim.startswith("ALPHA"))
        assert len(alpha.evidence) == 1
        assert alpha.confidence == 0.5 and alpha.S == 1

    def test_same_night_duplicate_claim_in_one_insight_not_double_counted(self, tmp_path):
        """extract_claims returning the same claim twice from ONE insight is a
        single assertion, not two rediscoveries."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "ok.md", body="MARKER-OK")
        llm = FakeLLM(
            claims_map={
                "MARKER-OK": [
                    {"claim": "ALPHA stable claim.", "summary": "first"},
                    {"claim": "ALPHA stable claim.", "summary": "second"},
                ]
            }
        )
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.created == 1 and result.merged == 0 and result.revised == 1
        page = load_all_pages(env["cortex_dir"])[0]
        assert len(page.evidence) == 1
        assert page.confidence == 0.5 and page.S == 1


# ── Verdict-driven actions ────────────────────────────────────────────


class TestActions:
    def _seed_alpha_page(self, tmp_path, env):
        """First night: create the ALPHA page."""
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A", sources=["Doc X"])
        llm = FakeLLM(
            claims_map={
                "MARKER-A": [
                    {"claim": "ALPHA: recall rewrites the memory trace.", "summary": "s1"}
                ],
            }
        )
        run_consolidation(llm, FakeRAG(), **env)
        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 1
        return pages[0]

    def test_equivalent_merges_not_creates(self, tmp_path):
        env = _env(tmp_path)
        first = self._seed_alpha_page(tmp_path, env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B", sources=["Doc Y"])
        llm = FakeLLM(
            claims_map={
                "MARKER-B": [
                    {"claim": "NEARALPHA: every retrieval re-encodes the trace.", "summary": "s2"},
                ]
            },
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
                claims_map={
                    f"MARKER-V{i}": [
                        {"claim": f"NEARALPHA variant {i} of the trace claim.", "summary": "s"},
                    ]
                },
                verdicts={(f"variant {i}", "ALPHA"): "equivalent"},
            )
            run_consolidation(llm, FakeRAG(), **env, max_variants=3)

        page = load_all_pages(env["cortex_dir"])[0]
        assert page.confidence == 0.9  # capped
        assert len(page.variants) == 3  # capped, oldest dropped
        # Spacing rule: seven same-night merges barely move S (R≈1 each time).
        assert 1.0 <= page.S < 1.1

    def test_contradicts_links_and_dents_both(self, tmp_path):
        env = _env(tmp_path)
        first = self._seed_alpha_page(tmp_path, env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-C")
        llm = FakeLLM(
            claims_map={
                "MARKER-C": [
                    {"claim": "NEARALPHA: recall never alters stored memories.", "summary": "s"},
                ]
            },
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
        assert old.confidence == 0.3  # 0.5 - 0.2
        assert new.confidence == 0.3

    def test_unrelated_creates_page_and_indexes(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(
            claims_map={"MARKER-A": [{"claim": "BETA: sleep consolidates memory.", "summary": "s"}]}
        )
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
        llm = FakeLLM(
            claims_map={
                "MARKER-A": [{"claim": claim, "summary": "s1"}],
                "MARKER-B": [{"claim": claim, "summary": "s2"}],
            }
        )
        result = run_consolidation(llm, FakeRAG(), **env)

        assert result.created == 1 and result.merged == 1
        assert llm.adjudicate_calls == []  # claim_id fast path
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
        assert result.created == 1  # falls through to new page

    def test_cache_hit_skips_llm_and_is_order_invariant(self, tmp_path):
        env = _env(tmp_path)
        assert _pair_key("A", "B") == _pair_key("B", "A")

        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA base claim.", "summary": "s"}]})
        run_consolidation(llm1, FakeRAG(), **env)

        # Night 2: adjudicated once (complementary → new page + link).
        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        claims_b = [{"claim": "NEARALPHA cousin claim.", "summary": "s"}]
        llm2 = FakeLLM(
            claims_map={"MARKER-B": claims_b}, verdicts={("NEARALPHA", "ALPHA"): "complementary"}
        )
        run_consolidation(llm2, FakeRAG(), **env)
        assert len(llm2.adjudicate_calls) == 1

        # Night 3: same pair re-appears (same claim text from a new insight)
        # → exact-duplicate path merges; no adjudication. Instead verify the
        # cache file kept the verdict for the live pair.
        cache = json.loads(env["cache_file"].read_text())
        assert any(v.get("verdict") == "complementary" for v in cache.values())

    def test_failed_adjudication_is_pending_not_permanently_cached(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        seed = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA base claim.", "summary": "s"}]})
        run_consolidation(seed, FakeRAG(), **env)

        class FailingAdjudicator(FakeLLM):
            def adjudicate_claims(self, claim_a, claim_b):
                self.adjudicate_calls.append((claim_a, claim_b))
                return {
                    "verdict": "unrelated",
                    "rationale": "adjudication failed; conservative default",
                    "valid": False,
                }

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        failed = FailingAdjudicator(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA close claim.", "summary": "s"}]}
        )
        run_consolidation(failed, FakeRAG(), **env)

        cache = json.loads(env["cache_file"].read_text())
        state = json.loads(env["state_file"].read_text())
        assert cache == {}  # synthetic unrelated must never become permanent
        assert state["pending_edges"]  # pair is owed and will be retried next run

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
        llm2 = FakeLLM(
            claims_map={"MARKER-B": [{"claim": "BETA new sibling claim.", "summary": "s"}]}
        )
        run_consolidation(llm2, FakeRAG(), **env)

        # Cache entry was recomputed for the edited claim text.
        state = json.loads(env["state_file"].read_text())
        entries = list(state["claim_embeddings"].values())
        assert any(e["embedding"] == [0.0, 1.0] for e in entries)  # BETA vector

    def test_stale_dim_cached_embedding_is_reembedded_not_crashed(self, tmp_path):
        """A model switch (bge-m3 768→1024) leaves stale-dim vectors in the
        embedding cache. Comparing a live-dim claim against them used to crash
        cosine; now they re-embed to the live width and the run survives."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA original claim.", "summary": "s"}]})
        run_consolidation(llm, FakeRAG(), **env)

        # Simulate an old-model embedding left behind by a dimension switch.
        state = json.loads(env["state_file"].read_text())
        cid = next(iter(state["claim_embeddings"]))
        state["claim_embeddings"][cid]["embedding"] = [0.1, 0.2, 0.3]  # 3-dim, stale
        env["state_file"].write_text(json.dumps(state), encoding="utf-8")

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = FakeLLM(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA cousin claim.", "summary": "s"}]}
        )
        result = run_consolidation(llm2, FakeRAG(), **env)

        assert result.status == "succeeded"  # no ValueError crash
        assert result.insights_processed == 1
        # The stale 3-dim vector was re-embedded back to the live 2-dim width.
        state = json.loads(env["state_file"].read_text())
        assert all(len(e["embedding"]) == 2 for e in state["claim_embeddings"].values())

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

    def test_vault_prompt_prefers_file_over_fallback(self, monkeypatch, tmp_path):
        # A1 (2026-07-13): Cortex prompts are externalized. _vault_prompt reads
        # the vault file when present, else the built-in fallback.
        import core.config as cfg

        pdir = tmp_path / "Prompts"
        pdir.mkdir()
        (pdir / "cortex_falsifiability.md").write_text("VAULT VERSION", encoding="utf-8")
        monkeypatch.setattr(cfg, "PROMPTS_DIR", pdir)
        client = LLMClient()
        assert client._vault_prompt("cortex_falsifiability.md", "FALLBACK") == "VAULT VERSION"
        # missing file → fallback (never breaks the nightly pipeline)
        assert client._vault_prompt("does_not_exist.md", "FALLBACK") == "FALLBACK"

    def test_extract_claims_parses_and_caps(self, monkeypatch):
        response = json.dumps(
            [
                {"claim": "Claim number one is long enough.", "summary": "s1"},
                {"claim": "Claim number two is long enough.", "summary": "s2"},
                {"claim": "Claim number three is long enough.", "summary": "s3"},
                {"claim": "Claim number four is long enough.", "summary": "s4"},
            ]
        )
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
        assert result.created == 0  # mock claims filtered out, no crash

    def test_adjudicate_valid_and_illegal(self, monkeypatch):
        ok = self._client(monkeypatch, '{"verdict": "Equivalent", "rationale": "r"}')
        result = ok.adjudicate_claims("a", "b")
        assert result["verdict"] == "equivalent" and result["valid"] is True

        bad = self._client(monkeypatch, '{"verdict": "kinda-similar"}')
        result = bad.adjudicate_claims("a", "b")
        assert result["verdict"] == "unrelated" and result["valid"] is False

        garbage = self._client(monkeypatch, "no json at all")
        result = garbage.adjudicate_claims("a", "b")
        assert result["verdict"] == "unrelated" and result["valid"] is False

    def test_adjudicate_rerolls_past_parse_miss(self, monkeypatch):
        # 2026-07-13 A3: a reasoning-channel parse miss on the first attempt must
        # RE-ROLL, not silently degrade to "unrelated" — that lost real merges
        # (equivalent is the merge trigger). Empty first, valid verdict on retry.
        from services.llm_client import LLMClient

        client = LLMClient()
        responses = iter(["", '{"verdict": "equivalent", "rationale": "r"}'])
        monkeypatch.setattr(client, "_complete_text", lambda *a, **k: next(responses))
        assert client.adjudicate_claims("a", "b")["verdict"] == "equivalent"

    def test_adjudicate_gives_up_after_attempts(self, monkeypatch):
        # Persistent unparseable → conservative "unrelated" after the re-rolls.
        client = self._client(monkeypatch, "never valid json")
        result = client.adjudicate_claims("a", "b")
        assert result["verdict"] == "unrelated" and result["valid"] is False


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
            page = self._run_one(tmp_path / f"s{score}", FalsifiabilityFakeLLM(score=score))
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
        assert page.confidence == 0.7  # 不是 3.3

    def test_llm_without_method_fails_open(self, tmp_path):
        # Phase 2 的 FakeLLM 沒有 assess_falsifiability —— 必須照常運作
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm = FakeLLM(
            claims_map={"MARKER-A": [{"claim": "BETA plain claim here.", "summary": "s"}]}
        )
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
        page = self._run_one(tmp_path, FalsifiabilityFakeLLM(), applies_when="處理長文拆解時")
        assert page.applies_when == "處理長文拆解時"

    def test_merge_path_never_reassesses(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = FalsifiabilityFakeLLM(
            claims_map={"MARKER-A": [{"claim": "ALPHA base claim text.", "summary": "s"}]}
        )
        run_consolidation(llm1, FakeRAG(), **env)
        assert len(llm1.assess_calls) == 1  # 建頁評一次

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = FalsifiabilityFakeLLM(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA same idea rephrased.", "summary": "s"}]},
            verdicts={("NEARALPHA", "ALPHA"): "equivalent"},
        )
        result = run_consolidation(llm2, FakeRAG(), **env)
        assert result.merged == 1
        assert llm2.assess_calls == []  # merge 路徑零額外 call


class TestWikilinkPenetration:
    def test_sources_mined_filtered_and_capped(self, tmp_path, monkeypatch):
        import maintenance.cortex_consolidation as cc

        pages_dir = tmp_path / "pages"
        notes_dir = tmp_path / "Notes"
        pages_dir.mkdir()
        notes_dir.mkdir()
        for name in ("P1", "P2", "P3", "P4", "P5", "P6"):
            (pages_dir / f"{name}.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(cc, "PAGES_DIR", pages_dir)
        monkeypatch.setattr(cc, "NOTES_DIR", notes_dir)

        env = _env(tmp_path)
        body = "MARKER-A 引用 [[P1]] [[P2|alias]] [[P3#sec]] [[Ghost]] [[P4]] [[P5]] [[P6]] [[P1]]"
        _write_insight(env["insights_dir"], "n1.md", body=body, sources=["P1"])
        llm = FalsifiabilityFakeLLM(
            claims_map={"MARKER-A": [{"claim": "BETA sourced claim text.", "summary": "s"}]}
        )
        run_consolidation(llm, FakeRAG(), **env)

        page = load_all_pages(env["cortex_dir"])[0]
        sources = page.evidence[0]["sources"]
        assert "Ghost" not in sources  # 存在性過濾
        assert len(sources) == 5  # 上限 5
        assert sources[0] == "P1"  # frontmatter 來源優先且去重


# ── O0: graph density (link/merge split, pending drain, backfill relink) ──


class TestO0GraphDensity:
    def _two_claim_env(self, tmp_path, mid_marker, verdict):
        """Insight A → ALPHA claim; insight B → <mid_marker> claim; one verdict."""
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "1_a.md", body="MARKER-A")
        _write_insight(env["insights_dir"], "2_b.md", body="MARKER-B")
        llm = FakeLLM(
            claims_map={
                "MARKER-A": [{"claim": "ALPHA base claim about X.", "summary": "s"}],
                "MARKER-B": [{"claim": f"{mid_marker} related claim about Y.", "summary": "s"}],
            },
            verdicts={("ALPHA base", f"{mid_marker} related"): verdict},
        )
        return env, llm

    def test_merge_guard_links_below_merge_floor(self, tmp_path):
        # equivalent verdict, but sim≈0.68 < 0.80 merge floor → link, don't merge.
        env, llm = self._two_claim_env(tmp_path, "GAMMA", "equivalent")
        run_consolidation(llm, FakeRAG(), **env)
        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 2  # NOT collapsed into one
        ids = {p.claim_id for p in pages}
        assert any(set(p.related) & (ids - {p.claim_id}) for p in pages)  # linked instead

    def test_merge_still_happens_above_merge_floor(self, tmp_path):
        # NEARALPHA sim≈0.95 ≥ 0.80 → equivalent genuinely merges.
        env, llm = self._two_claim_env(tmp_path, "NEARALPHA", "equivalent")
        run_consolidation(llm, FakeRAG(), **env)
        assert len(load_all_pages(env["cortex_dir"])) == 1

    def test_midrange_pair_is_adjudicated(self, tmp_path):
        # A 0.68 pair sat below the old 0.80 floor and was never adjudicated;
        # at the 0.60 link floor it now gets an edge.
        env, llm = self._two_claim_env(tmp_path, "GAMMA", "complementary")
        run_consolidation(llm, FakeRAG(), **env)
        assert any(
            ("ALPHA base" in a and "GAMMA related" in b)
            or ("ALPHA base" in b and "GAMMA related" in a)
            for a, b in llm.adjudicate_calls
        )
        pages = load_all_pages(env["cortex_dir"])
        assert sum(len(p.related) for p in pages) >= 2  # symmetric edge

    def test_pending_edges_stashed_then_drained(self, tmp_path):
        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "1_a.md", body="MARKER-A")
        _write_insight(env["insights_dir"], "2_b.md", body="MARKER-B")
        llm = FakeLLM(
            claims_map={
                "MARKER-A": [{"claim": "ALPHA base claim about X.", "summary": "s"}],
                "MARKER-B": [{"claim": "GAMMA related claim about Y.", "summary": "s"}],
            },
            verdicts={("ALPHA base", "GAMMA related"): "complementary"},
        )
        # Run 1: quota 0 → B enters with no edges, its neighbor A stashed.
        run_consolidation(llm, FakeRAG(), **env, max_adjudications=0)
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert state.get("pending_edges")  # something owed
        pages = load_all_pages(env["cortex_dir"])
        assert sum(len(p.related) for p in pages) == 0  # no edges yet

        # Run 2: quota available → drain runs first, edge appears.
        run_consolidation(llm, FakeRAG(), **env, max_adjudications=10)
        state = json.loads(env["state_file"].read_text(encoding="utf-8"))
        assert not state.get("pending_edges")  # drained
        pages = load_all_pages(env["cortex_dir"])
        assert sum(len(p.related) for p in pages) >= 2

    def test_relink_all_pages_backfill(self, tmp_path):
        # Seed two unrelated-at-ingest pages, then backfill-relink them.
        from maintenance.cortex_consolidation import _Consolidator

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "1_a.md", body="MARKER-A")
        _write_insight(env["insights_dir"], "2_b.md", body="MARKER-B")
        seed_llm = FakeLLM(
            claims_map={
                "MARKER-A": [{"claim": "ALPHA base claim about X.", "summary": "s"}],
                "MARKER-B": [{"claim": "GAMMA related claim about Y.", "summary": "s"}],
            },
            verdicts={},  # unrelated at ingest → no edges written
        )
        run_consolidation(seed_llm, FakeRAG(), **env, max_adjudications=0)
        assert sum(len(p.related) for p in load_all_pages(env["cortex_dir"])) == 0

        # Backfill with a verdict now available.
        relink_llm = FakeLLM(verdicts={("ALPHA base", "GAMMA related"): "complementary"})
        worker = _Consolidator(
            relink_llm,
            FakeRAG(),
            cortex_dir=env["cortex_dir"],
            state={},
            adjudication_cache={},
            max_adjudications=50,
            top_k=3,
            link_threshold=0.60,
            merge_threshold=0.80,
            max_variants=0,
        )
        stats = worker.relink_all_pages()
        assert stats["related"] >= 1
        assert sum(len(p.related) for p in load_all_pages(env["cortex_dir"])) >= 2

    def test_relink_dry_run_writes_nothing(self, tmp_path):
        from maintenance.cortex_consolidation import _Consolidator

        env = _env(tmp_path)
        _write_insight(env["insights_dir"], "1_a.md", body="MARKER-A")
        _write_insight(env["insights_dir"], "2_b.md", body="MARKER-B")
        seed_llm = FakeLLM(
            claims_map={
                "MARKER-A": [{"claim": "ALPHA base claim about X.", "summary": "s"}],
                "MARKER-B": [{"claim": "GAMMA related claim about Y.", "summary": "s"}],
            },
        )
        run_consolidation(seed_llm, FakeRAG(), **env, max_adjudications=0)
        relink_llm = FakeLLM(verdicts={("ALPHA base", "GAMMA related"): "complementary"})
        worker = _Consolidator(
            relink_llm,
            FakeRAG(),
            cortex_dir=env["cortex_dir"],
            state={},
            adjudication_cache={},
            max_adjudications=50,
            top_k=3,
            link_threshold=0.60,
            merge_threshold=0.80,
            max_variants=0,
        )
        stats = worker.relink_all_pages(dry_run=True)
        assert stats["pairs"] >= 1 and stats["adjudicated"] == 0
        assert not relink_llm.adjudicate_calls  # no LLM in dry-run
        assert sum(len(p.related) for p in load_all_pages(env["cortex_dir"])) == 0
