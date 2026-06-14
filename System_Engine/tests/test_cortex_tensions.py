"""Cortex Phase 5 F3: tension scan + TensionAgent rendering."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.cortex_tensions import scan_tensions
from services.cortex_store import CortexPage, make_claim_id, save_cortex_page


def _page(tmp_path, claim, **kw):
    cid = make_claim_id(claim)
    p = CortexPage(claim_id=cid, path=tmp_path / f"{cid}.md", claim=claim, **kw)
    save_cortex_page(p)
    return p


def _ev(name):
    return {"insight": name, "date": "2026-06-13", "summary": "s", "sources": []}


def test_contradictions_deduped_and_resolved(tmp_path):
    a = _page(tmp_path, "X always increases Y",
              evidence=[_ev("i1.md"), _ev("i2.md")])
    b = _page(tmp_path, "X has no effect on Y",
              evidence=[_ev("i3.md"), _ev("i4.md")])
    # Both sides record the contradiction; the scan must show the pair once.
    a.contradictions = [b.claim_id]; save_cortex_page(a)
    b.contradictions = [a.claim_id]; save_cortex_page(b)

    r = scan_tensions(tmp_path)
    assert len(r.contradictions) == 1                       # deduped A↔B
    page, others = r.contradictions[0]
    assert "X has no effect on Y" in others[0] or "X always increases Y" in others[0]  # id resolved to text


def test_dogmatic_bucket(tmp_path, monkeypatch):
    import services.cortex_tensions as ct
    monkeypatch.setattr(ct, "CORTEX_TENSION_DOGMATIC_FALS", 0.25)
    monkeypatch.setattr(ct, "CORTEX_TENSION_DOGMATIC_CONF", 0.5)
    _page(tmp_path, "Beauty is the highest value", confidence=0.7, falsifiability=0.0,
          evidence=[_ev("i1.md"), _ev("i2.md")])     # unfalsifiable + confident
    _page(tmp_path, "Cache cuts latency 40%", confidence=0.7, falsifiability=0.9,
          evidence=[_ev("i1.md"), _ev("i2.md")])     # falsifiable → not dogmatic
    r = scan_tensions(tmp_path)
    titles = [p.claim for p in r.dogmatic]
    assert "Beauty is the highest value" in titles
    assert "Cache cuts latency 40%" not in titles


def test_thin_evidence_bucket(tmp_path):
    _page(tmp_path, "single-source claim", evidence=[_ev("i1.md")])      # 1 source → thin
    _page(tmp_path, "no-source claim", evidence=[])                              # 0 → thin
    _page(tmp_path, "well-sourced claim",
          evidence=[_ev("i1.md"), _ev("i2.md")])                 # 2 → not thin
    r = scan_tensions(tmp_path)
    thin = {p.claim for p in r.thin_evidence}
    assert "single-source claim" in thin and "no-source claim" in thin
    assert "well-sourced claim" not in thin


def test_falsified_separated_not_double_counted(tmp_path):
    # A falsified claim is dead — it must NOT also appear in dogmatic/thin.
    _page(tmp_path, "disproven and unfalsifiable", status="falsified",
          confidence=0.7, falsifiability=0.0, evidence=[], counterpoints=["refuted by X"])
    r = scan_tensions(tmp_path)
    assert len(r.falsified) == 1
    assert r.dogmatic == [] and r.thin_evidence == []


def test_empty_cortex_no_tensions(tmp_path):
    r = scan_tensions(tmp_path)
    assert not r.any and r.total_pages == 0


# ── TensionAgent rendering ──────────────────────────────────────────────

def test_agent_render_sections_and_graveyard(tmp_path):
    from agents.tension_agent import TensionAgent
    agent = TensionAgent.__new__(TensionAgent)
    a = _page(tmp_path, "claim A", evidence=[_ev("i.md")])
    falsified = _page(tmp_path, "old belief", status="falsified", counterpoints=["killed by Y"])
    from services.cortex_tensions import TensionReport
    r = TensionReport(
        contradictions=[(a, ["claim B"])],
        dogmatic=[a],
        thin_evidence=[a],
        falsified=[falsified],
        total_pages=2,
    )
    body = agent._render(r)
    assert "矛盾對" in body and "↔ 與之衝突：claim B" in body
    assert "教條風險" in body
    assert "證據單薄" in body
    assert "已被推翻" in body and "~~old belief~~" in body and "killed by Y" in body


def test_agent_render_no_tensions(tmp_path):
    from agents.tension_agent import TensionAgent
    from services.cortex_tensions import TensionReport
    agent = TensionAgent.__new__(TensionAgent)
    body = agent._render(TensionReport(total_pages=3))
    assert "沒有偵測到張力" in body
