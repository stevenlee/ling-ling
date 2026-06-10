import sys
import json
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest
from services.insight_signals import compute_signals, InsightSignals

class FakeRAG:
    def __init__(self, indexed_titles):
        self._titles = set(indexed_titles)
        
    def get_all_indexed_titles(self):
        return self._titles
        
    def ef(self, texts):
        embs = []
        for t in texts:
            if "TargetA" in t:
                embs.append([1.0, 0.0])
            elif "TargetB" in t:
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
        
    def refute_insight(self, candidate, sources):
        if self.raise_err:
            raise Exception("fake llm error")
        self.called = True
        return {
            "verdict": self.verdict,
            "notes": "Some notes"
        }

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
    
    (pages_dir / "ExistingPage.md").write_text("")
    (notes_dir / "ExistingNote.md").write_text("")
    
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
    
    signals2 = compute_signals(report1, [], rag, llm)
    # Due to floating point math with dot products, it could be very close to 0
    assert signals2.novelty < 0.001
    assert signals2.max_similar_insight is not None

def test_bridging(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM()
    
    signals = compute_signals("report", ["TargetA", "TargetB"], rag, llm)
    assert signals.bridging == 1.0
    
    signals_zero = compute_signals("report", ["TargetA"], rag, llm)
    assert signals_zero.bridging == 0.0

def test_refute(patch_env):
    rag = FakeRAG([])
    llm = FakeLLM(verdict="refuted")
    
    signals = compute_signals("report", [], rag, llm, run_refute=True)
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
    assert signals.refute_verdict is None
