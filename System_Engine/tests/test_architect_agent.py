"""C3: ArchitectAgent — pre-scan feeds facts, path-A axes, routing."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import agents.architect_agent as arch_mod
from agents.architect_agent import ArchitectAgent
from services.command_dispatcher import detect_intent

_PACKED = """---
type: packed-code
source_paths:
  - System_Engine/services/foo.py
identifiers:
  - Foo
---

# Packed

## System_Engine/services/foo.py

```python
import re


class Foo:
    pass
```
"""


class _LLM:
    def __init__(self, report):
        self._report = report
        self.aq_kwargs = None

    def answer_query(self, **kw):
        self.aq_kwargs = kw
        return self._report


def _agent(llm):
    a = ArchitectAgent.__new__(ArchitectAgent)
    a.llm = llm
    a.stats = {"input_chars": 0, "output_chars": 0}
    a._write_report = lambda title, body, rtype, meta=None: (None, body)
    return a


def test_packed_flow_feeds_facts_and_axes(tmp_path, monkeypatch):
    monkeypatch.setattr(arch_mod, "CODE_REVIEW_DIR", tmp_path)
    (tmp_path / "foo.md").write_text(_PACKED, encoding="utf-8")
    llm = _LLM(report="## 系統概觀\n`foo` 模組。")
    agent = _agent(llm)
    out = agent.execute({"user_directive": "@ling-architect [[foo]]"})

    assert llm.aq_kwargs["persona"] == "coder"
    assert llm.aq_kwargs["operation"] == "map_architecture"
    assert llm.aq_kwargs["forced_template"] == "architecture-rpt"
    # the deterministic facts (not just raw code) reached the model
    assert "結構事實" in llm.aq_kwargs["wiki_context"]
    assert "classes: Foo" in llm.aq_kwargs["wiki_context"]
    assert out.startswith("## 系統概觀")


def test_missing_note_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(arch_mod, "CODE_REVIEW_DIR", tmp_path)
    agent = _agent(_LLM(report=""))
    out = agent.execute({"user_directive": "@ling-architect [[nope]]"})
    assert "make pack-code" in out


def test_routing_architect():
    assert detect_intent("@ling-architect", "@ling-architect") == "architect"
    assert detect_intent("x", "/architect [[m]]") == "architect"
