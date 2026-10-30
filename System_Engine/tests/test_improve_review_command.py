"""M3 rejection reason command plumbing, without writing live reports."""

import pytest

from agents.improve_agent import ImproveAgent
from services.improvement_store import make_proposal, save_proposal, get_proposal


@pytest.mark.parametrize(
    "reason", ["", "不要新增 parser 不讀取的欄位。", "do not approve other-proposal"]
)
def test_reject_command_keeps_optional_reason(tmp_path, monkeypatch, reason):
    from agents import improve_agent as module

    pending, rejected = tmp_path / "_pending", tmp_path / "_rejected"
    monkeypatch.setattr(module, "IMPROVEMENTS_PENDING_DIR", pending)
    monkeypatch.setattr(module, "IMPROVEMENTS_REJECTED_DIR", rejected)
    proposal = make_proposal(
        axis="Cortex 信念",
        target_path="Templates/Prompts/cortex_falsifiability.md",
        rationale="r",
        addressed_fixes=[],
        original_content="original",
        revised_content="revised",
    )
    save_proposal(proposal, pending)
    agent = ImproveAgent(llm=None)
    monkeypatch.setattr(agent, "_write_report", lambda title, body, kind: (None, body))
    response = agent.execute({"user_directive": f"reject {proposal['id']} {reason}".strip()})
    assert "已退回" in response
    record = get_proposal(proposal["id"], rejected)
    assert record.get("rejected_reason", "") == reason
    assert not list(pending.glob("*.json"))
