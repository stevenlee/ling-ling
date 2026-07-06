"""P3: a missing REQUIRED prompt must be observable, not silent.

BaseAgent._load_prompt stays fail-open (returns "" so an agent never crashes on
a missing template), but a missing *required* prompt means the caller silently
drops to a hardcoded fallback or an empty prompt — behavior drift with no signal.
`required=True` upgrades the miss to ERROR and records it in
``stats["missing_required_prompts"]`` so it shows up in the run's stats/trace.
"""

import logging
import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from agents.base_agent import BaseAgent
from core.config import PROMPTS_DIR

_ABSENT = "definitely_absent_prompt_xyz"  # never exists in PROMPTS_DIR


def _agent() -> BaseAgent:
    return BaseAgent(llm=None)


def test_required_missing_logs_error_and_records(caplog):
    agent = _agent()
    with caplog.at_level(logging.ERROR):
        out = agent._load_prompt(_ABSENT, required=True)
    assert out == ""  # still fail-open
    assert agent.stats.get("missing_required_prompts") == [f"{_ABSENT}.md"]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_optional_missing_warns_only(caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING):
        out = agent._load_prompt(_ABSENT)  # required defaults to False
    assert out == ""
    assert "missing_required_prompts" not in agent.stats
    # a warning, but NOT an error
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.skipif(
    not (PROMPTS_DIR / "mermaid_rules.md").exists(),
    reason="vault prompts not present",
)
def test_required_present_is_clean():
    agent = _agent()
    out = agent._load_prompt("mermaid_rules.md", required=True)
    assert out.strip()  # real content returned
    assert "missing_required_prompts" not in agent.stats
