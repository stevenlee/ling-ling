"""@ling-profiles command: subcommand parsing and end-to-end approve."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

import agents.profiles_agent as pa_mod
from agents.profiles_agent import ProfilesAgent
from services.profile_manager import ProfileManager, render_profile_markdown


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    profiles_dir = tmp_path / "Profiles"
    pending_dir = profiles_dir / "_pending"
    personas_dir = tmp_path / "Personas"
    templates_dir = tmp_path / "Templates"
    from_llm = tmp_path / "fromLingLing"
    profiles_dir.mkdir(parents=True)

    monkeypatch.setattr(pa_mod, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(pa_mod, "PROFILES_PENDING_DIR", pending_dir)
    monkeypatch.setattr(pa_mod, "PERSONAS_DIR", personas_dir)
    monkeypatch.setattr(pa_mod, "TEMPLATES_DIR", templates_dir)
    monkeypatch.setattr(pa_mod, "FROM_LLM_DIR", from_llm)

    agent = ProfilesAgent.__new__(ProfilesAgent)
    agent.llm = None
    agent.rag = None
    agent.stats = {"input_chars": 0, "output_chars": 0}
    # Capture reports instead of writing real files.
    reports = []
    agent._write_report = lambda title, body, rtype, meta=None: (
        reports.append((title, body)) or (tmp_path / "r.md", body)
    )
    return agent, reports, profiles_dir, personas_dir, templates_dir


class TestProfilesAgent:
    def test_default_lists_profiles(self, agent_env):
        agent, reports, profiles_dir, *_ = agent_env
        (profiles_dir / "patent.md").write_text(
            render_profile_markdown(persona="p", template="t", applicable_when="patents"),
            encoding="utf-8",
        )
        agent.execute({"user_directive": ""})
        title, body = reports[0]
        assert "patent" in body and "已生效" in body

    def test_pending_subcommand(self, agent_env):
        agent, reports, profiles_dir, *_ = agent_env
        pm = ProfileManager(profiles_dir)
        pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion",
            persona_content="x",
            template_name="diary-entry",
            template_content="y",
        )
        agent.execute({"user_directive": "show pending please"})
        _, body = reports[0]
        assert "diary" in body and "approve diary" in body

    def test_approve_subcommand_end_to_end(self, agent_env):
        agent, reports, profiles_dir, personas_dir, templates_dir = agent_env
        pm = ProfileManager(profiles_dir)
        pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion",
            persona_content="x",
            template_name="diary-entry",
            template_content="y",
        )
        agent.execute({"user_directive": "approve diary"})
        _, body = reports[0]
        assert "已生效" in body
        assert (personas_dir / "diary-companion.md").exists()
        assert (templates_dir / "diary-entry.md").exists()
        assert (profiles_dir / "diary.md").exists()

    def test_approve_unknown_reports_failure(self, agent_env):
        agent, reports, *_ = agent_env
        agent.execute({"user_directive": "approve ghost"})
        _, body = reports[0]
        assert "失敗" in body
