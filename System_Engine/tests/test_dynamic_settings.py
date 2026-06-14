"""Regression tests for core.config.DynamicSettings — the Scripture-driven
hot-reload mechanism.

The README promises that Scripture.md changes take effect immediately;
this suite locks that promise in by exercising DynamicSettings.reload()
against a tmp Scripture file (no daemon, no watchdog).
"""
import os
import sys
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

import core.config as config_mod
from core.config import DynamicSettings


def _write_scripture(tmp_path: Path, frontmatter: str) -> Path:
    f = tmp_path / "Scripture.md"
    f.write_text(dedent(frontmatter).lstrip("\n"), encoding="utf-8")
    return f


class TestDynamicSettingsDefaults:
    def test_constructor_supplies_baseline_values(self):
        s = DynamicSettings()
        assert s.AGENT_ROLE == "assistant"
        assert s.OUTPUT_LANGUAGE == "Traditional Chinese"
        assert s.USE_TEMPLATE is None
        assert s.STRICT_MODE is True
        assert 0.0 < s.CREATIVITY <= 1.0
        assert s.DIGEST_LIMIT > 0
        assert 0 <= s.DREAMING_FROM <= 23
        assert 0 <= s.DREAMING_TO <= 23

    def test_reload_with_missing_file_keeps_defaults(self, tmp_path, monkeypatch):
        # Point SCRIPTURE_FILE at a non-existent path.
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", tmp_path / "no-such-file.md")
        s = DynamicSettings()
        before = (s.AGENT_ROLE, s.CREATIVITY)
        s.reload()
        after = (s.AGENT_ROLE, s.CREATIVITY)
        assert before == after


class TestDynamicSettingsReload:
    def test_loads_full_scripture(self, tmp_path, monkeypatch):
        f = _write_scripture(tmp_path, """
            ---
            be_a: researcher
            use_template: tech-rpt
            say: English
            digest_limit: 12000
            digest_overlap: 800
            dreaming_from: 2
            dreaming_to: 6
            self_healing: false
            creativity: 0.7
            max_output: 6000
            memory_limit: 64000
            search_depth: 5
            strict_mode: false
            ---
            # Scripture body (ignored by parser)
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)

        s = DynamicSettings()
        s.reload()
        assert s.AGENT_ROLE == "researcher"        # lowered
        assert s.OUTPUT_LANGUAGE == "English"
        assert s.USE_TEMPLATE == "tech-rpt"
        assert s.DIGEST_LIMIT == 12000
        assert s.DIGEST_OVERLAP == 800
        assert s.DREAMING_FROM == 2
        assert s.DREAMING_TO == 6
        assert s.SELF_HEALING is False
        assert s.CREATIVITY == 0.7
        assert s.MAX_OUTPUT == 6000
        assert s.MEMORY_LIMIT == 64000
        assert s.SEARCH_DEPTH == 5
        assert s.STRICT_MODE is False

    def test_phase6_learning_aid_toggles_hot_reload(self, tmp_path, monkeypatch):
        """Phase 6 output preferences live in Scripture (not .env) so creators
        flip them without a daemon restart. Default off → on after reload."""
        s = DynamicSettings()
        assert s.VISUAL_ROUTER_ENABLED is False
        assert s.ARGUMENT_MAP_MERMAID is False
        f = _write_scripture(tmp_path, """
            ---
            be_a: assistant
            visual_router: true
            argument_map_mermaid: true
            ---
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s.reload()
        assert s.VISUAL_ROUTER_ENABLED is True
        assert s.ARGUMENT_MAP_MERMAID is True

    def test_agent_role_is_lowercased(self, tmp_path, monkeypatch):
        f = _write_scripture(tmp_path, """
            ---
            be_a: TRANSLATOR
            ---
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        s.reload()
        assert s.AGENT_ROLE == "translator"

    def test_missing_keys_keep_existing_values(self, tmp_path, monkeypatch):
        f = _write_scripture(tmp_path, """
            ---
            be_a: coder
            ---
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        previous_creativity = s.CREATIVITY
        previous_digest = s.DIGEST_LIMIT
        s.reload()
        assert s.AGENT_ROLE == "coder"
        # Untouched by frontmatter → still defaults
        assert s.CREATIVITY == previous_creativity
        assert s.DIGEST_LIMIT == previous_digest

    def test_bad_value_is_skipped_not_fatal(self, tmp_path, monkeypatch, caplog):
        f = _write_scripture(tmp_path, """
            ---
            be_a: assistant
            creativity: "wildly creative"
            digest_limit: 10000
            ---
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        default_creativity = s.CREATIVITY
        with caplog.at_level("WARNING"):
            s.reload()
        # Bad value → keep prior default, do not crash
        assert s.CREATIVITY == default_creativity
        # Other valid keys still applied
        assert s.DIGEST_LIMIT == 10000
        assert any("creativity" in r.message for r in caplog.records)

    def test_empty_frontmatter_logs_warning(self, tmp_path, monkeypatch, caplog):
        # Frontmatter markers with a blank line in between → regex matches,
        # yaml.safe_load returns None → hits the "frontmatter is empty" branch.
        f = _write_scripture(tmp_path, """
            ---

            ---
            body
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        before = (s.AGENT_ROLE, s.CREATIVITY)
        with caplog.at_level("WARNING"):
            s.reload()
        assert (s.AGENT_ROLE, s.CREATIVITY) == before
        assert any("empty" in r.message.lower() for r in caplog.records)

    def test_no_frontmatter_logs_warning(self, tmp_path, monkeypatch, caplog):
        f = tmp_path / "Scripture.md"
        f.write_text("# Scripture\n\nJust body, no YAML.", encoding="utf-8")
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        with caplog.at_level("WARNING"):
            s.reload()
        assert any("frontmatter" in r.message.lower() for r in caplog.records)

    def test_reload_is_idempotent_and_repeatable(self, tmp_path, monkeypatch):
        """Editing Scripture.md and calling reload again must pick up the new
        values — this is the hot-reload promise."""
        f = _write_scripture(tmp_path, """
            ---
            be_a: assistant
            creativity: 0.3
            ---
        """)
        monkeypatch.setattr(config_mod, "SCRIPTURE_FILE", f)
        s = DynamicSettings()
        s.reload()
        assert s.AGENT_ROLE == "assistant"
        assert s.CREATIVITY == 0.3

        # Edit Scripture, reload, observe new values.
        f.write_text(dedent("""
            ---
            be_a: researcher
            creativity: 0.9
            ---
        """).lstrip("\n"), encoding="utf-8")
        s.reload()
        assert s.AGENT_ROLE == "researcher"
        assert s.CREATIVITY == 0.9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
