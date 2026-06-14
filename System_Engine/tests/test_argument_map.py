"""Phase 6 axis (3): argument map (Toulmin) — extraction + rendering."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.argument_map import build_argument_map, render_argument_map


class FakeLLM:
    def __init__(self, ret):
        self._ret = ret

    def _complete_json(self, **kw):
        return self._ret


_TOULMIN = {
    "claim": "Remote work raises productivity",
    "grounds": ["A 2021 study found 13% more output"],
    "warrants": ["the study population generalizes to all knowledge work"],
    "qualifier": "for self-directed tasks",
    "rebuttals": ["collaboration-heavy roles may suffer"],
    "weakest_link": "the generalization warrant is unstated and contestable",
}


def test_build_parses_toulmin_fields():
    out = build_argument_map(FakeLLM(_TOULMIN), "remote work essay")
    assert out["claim"].startswith("Remote work")
    assert out["warrants"] == ["the study population generalizes to all knowledge work"]
    assert out["qualifier"] == "for self-directed tasks"


def test_build_empty_claim_returns_empty():
    out = build_argument_map(FakeLLM({"claim": "  ", "grounds": ["x"]}), "rambling prose")
    assert out == {}


def test_build_empty_content_returns_empty():
    assert build_argument_map(FakeLLM(_TOULMIN), "   ") == {}


def test_render_surfaces_warrant_and_weakest_link():
    body = render_argument_map(_TOULMIN)
    assert "## 🧩 論證結構" in body
    assert "Remote work raises productivity" in body
    # The hidden-logic value-add must be its own labelled section.
    assert "隱含前提" in body and "generalizes to all knowledge work" in body
    assert "反駁" in body and "collaboration-heavy" in body
    assert "最弱的一環" in body and "contestable" in body


def test_render_empty_is_blank():
    assert render_argument_map({}) == ""
    assert render_argument_map({"claim": ""}) == ""


def test_router_dispatches_argument_map():
    # build_artifact with forced argument_map → routes through build+render.
    from services.learning_artifacts import build_artifact
    out = build_artifact(FakeLLM(_TOULMIN), "an argumentative essay", forced_type="argument_map")
    assert out["type"] == "argument_map"
    assert "隱含前提" in out["artifact"]


# ── optional Mermaid (follow-on C) ───────────────────────────────────────

def test_render_no_mermaid_by_default():
    body = render_argument_map(_TOULMIN)
    assert "```mermaid" not in body


def test_render_with_mermaid_appends_deterministic_graph():
    body = render_argument_map(_TOULMIN, with_mermaid=True)
    assert "```mermaid" in body and "graph TD" in body
    # grounds → solid edge to claim; warrants/rebuttals → dashed edges.
    assert "--> C" in body and "-. 未明說 .-> C" in body and "-. 挑戰 .-> C" in body


def test_mermaid_label_sanitized():
    from services.argument_map import _argument_mermaid
    data = {"claim": 'has "quotes"\nand newline', "grounds": [], "warrants": [], "rebuttals": []}
    mm = _argument_mermaid(data)
    # double-quotes downgraded to single, newline collapsed → valid node label.
    assert '"quotes"' not in mm and "'quotes'" in mm and "\nand newline" not in mm.split("\n", 2)[2]


def test_router_respects_argument_map_mermaid_flag(monkeypatch):
    from services.learning_artifacts import build_artifact
    monkeypatch.setattr("core.config.settings.ARGUMENT_MAP_MERMAID", True)
    out = build_artifact(FakeLLM(_TOULMIN), "essay", forced_type="argument_map")
    assert "```mermaid" in out["artifact"]
