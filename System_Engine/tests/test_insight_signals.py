import json


import pytest
from services.insight_signals import compute_signals
from services.llm_client import LLMClient


class FakeRAG:
    def __init__(self, indexed_titles):
        self._titles = set(indexed_titles)

    def get_all_indexed_titles(self):
        return self._titles

    def ef(self, texts):
        embs = []
        for t in texts:
            if "TargetA content" in t:
                embs.append([1.0, 0.0])
            elif "TargetB content" in t:
                embs.append([0.0, 1.0])
            elif "novel" in t:
                embs.append([-1.0, 0.0])
            else:
                embs.append([0.707, 0.707])
        return embs


class FakeLLM:
    def __init__(self, verdict="survived"):
        self.verdict = verdict
        self.called = False
        self.raise_err = False

    def refute_insight(self, candidate, sources, *, lenient=False, candidate_kind=None):
        if self.raise_err:
            raise Exception("fake llm error")
        self.called = True
        self.last_lenient = lenient
        self.last_kind = candidate_kind
        return {"verdict": self.verdict, "notes": "Some notes"}


@pytest.fixture
def patch_env(tmp_path, monkeypatch):
    import core.config
    import services.insight_signals

    db_dir = tmp_path / "Database"
    db_dir.mkdir()
    signals_file = db_dir / "insight_signals.json"

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    notes_dir = tmp_path / "Notes"
    notes_dir.mkdir()

    (pages_dir / "ExistingPage.md").write_text("Existing content")
    (notes_dir / "ExistingNote.md").write_text("Existing note content")

    (pages_dir / "TargetA.md").write_text("TargetA content")
    (pages_dir / "TargetB.md").write_text("TargetB content")

    monkeypatch.setattr(core.config, "INSIGHT_SIGNALS_FILE", signals_file)
    monkeypatch.setattr(services.insight_signals, "INSIGHT_SIGNALS_FILE", signals_file)

    monkeypatch.setattr(core.config, "PAGES_DIR", pages_dir)
    monkeypatch.setattr(services.insight_signals, "PAGES_DIR", pages_dir)
    monkeypatch.setattr(core.config, "NOTES_DIR", notes_dir)
    monkeypatch.setattr(services.insight_signals, "NOTES_DIR", notes_dir)

    monkeypatch.setattr(core.config, "INSIGHT_SIGNALS_ENABLED", True)
    monkeypatch.setattr(services.insight_signals, "INSIGHT_SIGNALS_ENABLED", True)

    monkeypatch.setattr(core.config, "INSIGHT_REFUTE_ENABLED", True)
    monkeypatch.setattr(services.insight_signals, "INSIGHT_REFUTE_ENABLED", True)

    return signals_file


# -- tests --


def test_groundedness_and_broken_links(patch_env):
    rag = FakeRAG(["IndexedDoc"])
    llm = FakeLLM()

    report = "This is [[ExistingPage|alias]], [[IndexedDoc]], [[ExistingNote]], and [[MissingDoc]]."
    signals = compute_signals(report, [], rag, llm)

    assert "MissingDoc" in signals.broken_links
    assert len(signals.broken_links) == 1
    assert signals.groundedness == 0.75


def test_groundedness_no_links(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM()
    report = "No links here."
    signals = compute_signals(report, [], rag, llm)
    assert signals.groundedness == 1.0
    assert not signals.broken_links


def test_novelty_and_sidecar(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM()

    report1 = "novel stuff here"
    signals1 = compute_signals(report1, [], rag, llm)
    assert signals1.novelty == 1.0

    # Recomputing the SAME content must not self-match (backfill re-sign,
    # retries): its own sidecar entry is excluded by content-hash id.
    signals_same = compute_signals(report1, [], rag, llm)
    assert signals_same.novelty == 1.0

    # A different report with an identical embedding (FakeRAG maps any
    # "novel"-containing text to the same vector) is near-zero novelty.
    signals2 = compute_signals("novel other things", [], rag, llm)
    # Due to floating point math with dot products, it could be very close to 0
    assert signals2.novelty < 0.001
    assert signals2.max_similar_insight is not None


def test_bridging(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM()

    # TargetA and TargetB exist in pages_dir
    signals = compute_signals("report", ["TargetA", "TargetB"], rag, llm)
    assert signals.bridging == 1.0

    signals_zero = compute_signals("report", ["TargetA"], rag, llm)
    assert signals_zero.bridging == 0.0


def test_refute(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM(verdict="refuted")

    signals = compute_signals("report", ["TargetA"], rag, llm, run_refute=True)
    assert signals.refute_verdict == "refuted"
    assert llm.called

    llm2 = FakeLLM(verdict="survived")
    signals2 = compute_signals("report", [], rag, llm2, run_refute=False)
    assert signals2.refute_verdict is None
    assert not llm2.called


def test_fail_open(patch_env):
    llm = FakeLLM()
    llm.raise_err = True

    signals = compute_signals("report", [], None, llm)
    # Fail open semantics: should be None, not 0.0
    assert signals.refute_verdict is None
    assert signals.groundedness == 1.0  # no links = 1.0
    assert signals.bridging is None


def test_bridging_nested_pages(patch_env):
    """Pages live in per-document folders (pages/<Doc>/<Doc> (Synthesis).md);
    source lookup must resolve them, not just flat pages/*.md."""
    import services.insight_signals as mod

    nested_a = mod.PAGES_DIR / "DocA"
    nested_a.mkdir()
    (nested_a / "DocA (Synthesis).md").write_text("TargetA content")
    nested_b = mod.PAGES_DIR / "DocB"
    nested_b.mkdir()
    (nested_b / "DocB (Synthesis).md").write_text("TargetB content")

    rag = FakeRAG([])
    llm = FakeLLM()
    signals = compute_signals("report", ["DocA (Synthesis)", "DocB (Synthesis)"], rag, llm)
    assert signals.bridging == 1.0
    # refute also depends on the same source lookup
    assert signals.refute_verdict == "survived"
    assert llm.called


def test_groundedness_nested_pages(patch_env):
    import services.insight_signals as mod

    nested = mod.PAGES_DIR / "DocA"
    nested.mkdir()
    (nested / "DocA (Synthesis).md").write_text("TargetA content")

    rag = FakeRAG([])
    llm = FakeLLM()
    signals = compute_signals("See [[DocA (Synthesis)]] and [[Missing]].", [], rag, llm)
    assert signals.groundedness == 0.5
    assert signals.broken_links == ["Missing"]


def test_novelty_survives_embedding_dim_change(patch_env):
    """Sidecar entries from an older embedding model (different dim) must be
    purged, not crash the novelty computation."""
    stale = {
        f"old_{i}": {"embedding": [0.1, 0.2, 0.3], "ts": f"2000-01-01T00:00:{i:02d}"}
        for i in range(3)
    }
    patch_env.write_text(json.dumps(stale))

    rag = FakeRAG([])  # FakeRAG.ef returns 2-dim embeddings
    llm = FakeLLM()

    signals = compute_signals("novel stuff here", [], rag, llm)
    assert signals.novelty == 1.0  # no comparable history left

    history = json.loads(patch_env.read_text())
    assert len(history) == 1  # stale 3-dim entries purged, new entry saved
    assert all(len(v["embedding"]) == 2 for v in history.values())


# -- S2 Tests --


def test_sidecar_limit_500(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM()

    # create 500 fake records
    data = {}
    for i in range(500):
        data[f"id_{i}"] = {"embedding": [0.0, 1.0], "ts": f"2000-01-01T00:00:{i % 60:02d}"}
    patch_env.write_text(json.dumps(data))

    # Add 1 more via compute_signals
    compute_signals("new stuff", [], rag, llm)

    history = json.loads(patch_env.read_text())
    assert len(history) == 500
    assert "id_0" not in history  # Oldest is evicted


def test_sidecar_corruption_recovery(patch_env):
    patch_env.write_text("invalid json { {[")
    rag = FakeRAG([])
    llm = FakeLLM()

    # Should not crash, and should rebuild file
    compute_signals("novel stuff here", [], rag, llm)
    history = json.loads(patch_env.read_text())
    assert len(history) == 1


def test_flag_off(patch_env, monkeypatch):
    import core.config
    import services.insight_signals

    monkeypatch.setattr(core.config, "INSIGHT_SIGNALS_ENABLED", False)
    monkeypatch.setattr(services.insight_signals, "INSIGHT_SIGNALS_ENABLED", False)

    rag = FakeRAG([])
    llm = FakeLLM()

    signals = compute_signals("report", ["TargetA", "TargetB"], rag, llm)
    assert signals.groundedness is None
    assert signals.novelty is None
    assert signals.bridging is None
    assert signals.refute_verdict is None


# -- M2 Tests --


def test_llm_client_refute_regex(monkeypatch):
    client = LLMClient()

    class FakeResponse:
        def __init__(self, text):
            self.text = text

    def mock_complete_text(*args, **kwargs):
        return kwargs.get("mock_response_text", "")

    monkeypatch.setattr(client, "_complete_text", mock_complete_text)

    # Helper to test different formatting
    def check_verdict(text, expected):
        monkeypatch.setattr(client, "_complete_text", lambda *a, **kw: text)
        res = client.refute_insight("c", ["s"])
        assert res["verdict"] == expected

    check_verdict("**Verdict:** refuted", "refuted")
    check_verdict("*Verdict*: refuted", "refuted")
    check_verdict("Verdict：survived", "survived")
    check_verdict("Some notes.\nVerdict: survived\n", "survived")
    check_verdict("Verdict :   REFUTED", "refuted")
    check_verdict("Just random text without verdict", None)


# -- Takeover addition (review R2): frontmatter block + mirror identity --


def test_signals_block_in_frontmatter_and_mirror_identity(patch_env, tmp_path, monkeypatch):
    """With signals enabled, the report frontmatter carries the signals
    block, and the Insights/ mirror stays byte-identical (brief §2.2)."""
    from agents.insight_agent import InsightAgent
    import agents.base_agent as base_agent_mod
    from core.parser import parse_markdown_metadata

    from_llm_dir = tmp_path / "fromLingLing"
    insights_dir = tmp_path / "Insights"
    from_llm_dir.mkdir()
    insights_dir.mkdir()
    monkeypatch.setattr(base_agent_mod, "FROM_LLM_DIR", from_llm_dir)

    class _StubLLM:
        model = "stub"

    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = _StubLLM()
    agent.rag = None  # signals fail-open to None values, block still present
    agent.stats = {"input_chars": 0, "output_chars": 0}
    agent.insights_dir = insights_dir
    agent.strategies = {
        "recency": {"name": "Recency", "description": "d", "pipeline": "single"},
    }
    monkeypatch.setattr(agent, "_run_single", lambda *a, **k: "## Insight body\n\nText.")

    full_markdown = agent.generate_insight("recency", target_titles=["Test Source"])

    meta = parse_markdown_metadata(full_markdown)
    assert "signals" in meta and meta["signals_version"] == 1
    assert set(meta["signals"]) == {"groundedness", "novelty", "bridging", "refute_verdict"}

    mirrored = list(insights_dir.glob("*.md"))
    assert len(mirrored) == 1
    assert mirrored[0].read_text(encoding="utf-8") == full_markdown


# ── Refute leniency for non-literal operations (2026-07-12 audit) ──


def test_compute_signals_threads_lenient(patch_env):
    """compute_signals must forward refute_lenient/refute_kind to the LLM so
    non-literal operations (analogy, fable, ...) aren't refuted on their
    self-declared limitations."""
    import services.insight_signals as mod

    nested = mod.PAGES_DIR / "DocA"
    nested.mkdir()
    (nested / "DocA (Synthesis).md").write_text("content")

    rag = FakeRAG([])
    llm = FakeLLM()
    compute_signals(
        "report",
        ["DocA (Synthesis)"],
        rag,
        llm,
        refute_lenient=True,
        refute_kind="analogy",
    )
    assert llm.last_lenient is True
    assert llm.last_kind == "analogy"


def test_llm_client_refute_lenient_injects_guardrail(monkeypatch):
    client = LLMClient()
    captured = {}

    def fake_complete(system, user, **kw):
        captured["user"] = user
        return "Verdict: survived"

    monkeypatch.setattr(client, "_complete_text", fake_complete)
    monkeypatch.setattr(client, "_build_system_prompt", lambda *a, **kw: ("SYS", {}))

    # Strict (default): no guardrail.
    client.refute_insight("candidate", ["src"])
    assert "non-literal operation" not in captured["user"]

    # Lenient: guardrail with the operation name + tear-line protection.
    client.refute_insight("candidate", ["src"], lenient=True, candidate_kind="analogy")
    assert "non-literal operation (analogy)" in captured["user"]
    assert "tear lines" in captured["user"]
    assert "transferable principle" in captured["user"]


def test_signals_meta_lenient_from_config(monkeypatch, tmp_path):
    """_signals_meta reads `refute_mode: lenient` from the skill config and
    passes it (with the operation name) into compute_signals."""
    from agents.insight.report_output import ReportOutputMixin

    monkeypatch.setattr("core.config.INSIGHT_SIGNALS_ENABLED", True, raising=False)

    seen = {}

    def fake_compute(content, titles, rag, llm, **kw):
        seen.update(kw)

        class S:
            groundedness = novelty = bridging = 0.5
            refute_verdict = "survived"

        return S()

    monkeypatch.setattr("services.insight_signals.compute_signals", fake_compute)

    obj = ReportOutputMixin()
    obj.rag = None
    obj.llm = None

    # lenient op
    obj._signals_meta("body", [], {"name": "fable", "refute_mode": "lenient"})
    assert seen["refute_lenient"] is True and seen["refute_kind"] == "fable"

    # literal op (montecarlo) → strict
    seen.clear()
    obj._signals_meta("body", [], {"name": "montecarlo"})
    assert seen["refute_lenient"] is False and seen["refute_kind"] is None

    # full-insight (config=None) → strict
    seen.clear()
    obj._signals_meta("body", [], None)
    assert seen["refute_lenient"] is False
