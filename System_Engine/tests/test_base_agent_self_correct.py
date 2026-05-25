"""Regression: BaseAgent._self_correct must handle structured quality_fixes.

A3 changed quality_fixes from list[str] to list[dict] but the log-line
in _self_correct kept doing `', '.join(fixes)`, which raised
"sequence item 0: expected str instance, dict found" the moment a real
report body contained anything the markdown quality pipeline wanted to
repair. The nightly full-insight scheduler tripped this for ~24h before
detection — see Database/maintenance_state.json.
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from agents.base_agent import BaseAgent


class _StubLLM:
    """Bare minimum to satisfy BaseAgent.__init__."""
    pass


def test_self_correct_handles_structured_fixes_without_crashing(caplog):
    """Body containing real LaTeX commands and a stray mermaid block —
    the pipeline produces dict-shaped fixes; the log line must extract
    type names before joining."""
    agent = BaseAgent(_StubLLM())

    # Body with content the quality pipeline will repair:
    #   - \binom (JSON-decode collision → repaired_latex_backspace)
    #   - bare mermaid keyword (wrapped_bare_mermaid)
    body = (
        "# Title\n\n"
        "Some formula: $\x08inom{n}{k}$.\n\n"
        "mermaid\ngraph TD\n  A --> B\n\n"
        "Some trailing whitespace.   \n"
    )

    with caplog.at_level(logging.INFO):
        cleaned = agent._self_correct(body)

    # Repairs landed in the output
    assert "\\binom" in cleaned
    assert "```mermaid" in cleaned

    # The log line MUST have produced a comma-separated string of fix
    # types, not crashed mid-join.
    log_messages = " ".join(r.message for r in caplog.records)
    assert "Applied markdown quality fixes" in log_messages
    assert "repaired_latex_backspace" in log_messages
    assert "wrapped_bare_mermaid" in log_messages


def test_self_correct_with_empty_fixes_logs_nothing(caplog):
    agent = BaseAgent(_StubLLM())
    with caplog.at_level(logging.INFO):
        agent._self_correct("Plain prose with nothing to repair.")
    assert not any(
        "Applied markdown quality fixes" in r.message for r in caplog.records
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
