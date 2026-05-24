"""Tests for services.capability_manager — pure logic, no LLM."""
import os
import sys
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.capability_manager import (
    CapabilityManager,
    CapabilitySpec,
    _as_str_tuple,
    _normalize_cost_class,
    _parse_capability_file,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip("\n"), encoding="utf-8")
    return path


def _make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    ops = tmp_path / "Operations"
    skills = tmp_path / "Skills"
    ops.mkdir()
    skills.mkdir()
    return ops, skills


# ── _as_str_tuple ────────────────────────────────────────────────────

class TestAsStrTuple:
    def test_none_returns_empty(self):
        assert _as_str_tuple(None) == ()

    def test_string_wraps(self):
        assert _as_str_tuple("foo") == ("foo",)

    def test_list_stringifies(self):
        assert _as_str_tuple(["a", "b", 3]) == ("a", "b", "3")

    def test_drops_none_elements(self):
        assert _as_str_tuple(["a", None, "b"]) == ("a", "b")

    def test_garbage_returns_empty(self):
        assert _as_str_tuple({"a": 1}) == ()


# ── _normalize_cost_class ────────────────────────────────────────────

class TestNormalizeCostClass:
    @pytest.mark.parametrize("value,expected", [
        ("low", "low"),
        ("MEDIUM", "medium"),
        (" High ", "high"),
        ("unknown", "unknown"),
    ])
    def test_known_values(self, value, expected):
        assert _normalize_cost_class(value) == expected

    @pytest.mark.parametrize("value", ["", "huge", None, 5, {"x": 1}])
    def test_unknown_falls_back(self, value):
        assert _normalize_cost_class(value) == "unknown"


# ── _parse_capability_file ───────────────────────────────────────────

class TestParseCapabilityFile:
    def test_full_frontmatter(self, tmp_path):
        path = _write(tmp_path / "foo.md", """
            ---
            type: operation
            description: Combine inputs.
            expected_inputs:
              - part_digests
            expected_context:
              - title
            produces:
              - synthesis_text
            cost_class: medium
            methodology: fixed
            applicable_when:
              database_populated: true
            ---

            body text
        """)
        spec = _parse_capability_file(path, fallback_type="operation")
        assert spec.name == "foo"  # file stem is canonical
        assert spec.type == "operation"
        assert spec.description == "Combine inputs."
        assert spec.expected_inputs == ("part_digests",)
        assert spec.expected_context == ("title",)
        assert spec.produces == ("synthesis_text",)
        assert spec.cost_class == "medium"
        assert spec.applicable_when == {"database_populated": True}
        assert spec.raw_frontmatter["methodology"] == "fixed"

    def test_missing_frontmatter_returns_empty_spec(self, tmp_path):
        path = _write(tmp_path / "bare.md", "No frontmatter, just body.\n")
        spec = _parse_capability_file(path, fallback_type="operation")
        assert spec.name == "bare"
        assert spec.type == "operation"  # fallback applied
        assert spec.expected_inputs == ()
        assert spec.cost_class == "unknown"

    def test_malformed_yaml_returns_empty_spec(self, tmp_path, caplog):
        path = _write(tmp_path / "broken.md", """
            ---
            type: operation
            expected_inputs:
              - foo
              -- bad indentation
            ---
            body
        """)
        with caplog.at_level("WARNING"):
            spec = _parse_capability_file(path, fallback_type="skill")
        assert spec.name == "broken"
        assert spec.type == "skill"  # fallback
        assert spec.expected_inputs == ()
        assert any("bad YAML" in r.message for r in caplog.records)

    def test_filestem_overrides_frontmatter_name(self, tmp_path):
        # frontmatter says name:bar but filename is foo — file stem wins
        path = _write(tmp_path / "foo.md", """
            ---
            name: bar
            type: skill
            description: stuff
            ---
            body
        """)
        spec = _parse_capability_file(path, fallback_type="skill")
        assert spec.name == "foo"
        assert spec.raw_frontmatter["name"] == "bar"  # preserved for legacy readers

    def test_non_mapping_frontmatter_falls_back(self, tmp_path):
        # YAML that parses to a list, not a dict
        path = _write(tmp_path / "weird.md", """
            ---
            - just
            - a
            - list
            ---
            body
        """)
        spec = _parse_capability_file(path, fallback_type="operation")
        assert spec.type == "operation"
        assert spec.expected_inputs == ()


# ── CapabilityManager ────────────────────────────────────────────────

class TestCapabilityManager:
    def test_scans_both_dirs(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        _write(ops / "synth.md", """
            ---
            type: operation
            description: x
            cost_class: medium
            ---
            body
        """)
        _write(skills / "recency.md", """
            ---
            type: skill
            description: y
            cost_class: low
            ---
            body
        """)
        mgr = CapabilityManager(ops, skills)
        assert {s.name for s in mgr.all()} == {"synth", "recency"}
        assert mgr.get("synth").type == "operation"
        assert mgr.get("recency").type == "skill"

    def test_missing_dirs_dont_raise(self, tmp_path):
        mgr = CapabilityManager(tmp_path / "nope1", tmp_path / "nope2")
        assert mgr.all() == []

    def test_localized_variants_skipped(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        _write(ops / "synth.md", "---\ntype: operation\n---\nbody")
        _write(ops / "synth.zh.md", "---\ntype: operation\n---\nbody zh")
        _write(ops / "synth.ja.md", "---\ntype: operation\n---\nbody ja")
        mgr = CapabilityManager(ops, skills)
        # Only the base file registers; localized variants share the canonical id.
        assert [s.name for s in mgr.all()] == ["synth"]

    def test_get_unknown_returns_none(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        mgr = CapabilityManager(ops, skills)
        assert mgr.get("nope") is None
        assert mgr.get("") is None
        assert mgr.get(None) is None

    def test_resolve_shape_all_axes(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        _write(ops / "synthesize.md", """
            ---
            type: operation
            description: x
            produces:
              - synthesis_text
            cost_class: medium
            ---
            body
        """)
        mgr = CapabilityManager(ops, skills)
        record = mgr.resolve(persona="translator", operation="synthesize", template="wiki-note")
        assert record["operation"]["name"] == "synthesize"
        assert record["operation"]["found"] is True
        assert record["operation"]["cost_class"] == "medium"
        assert record["operation"]["produces"] == ["synthesis_text"]
        assert record["persona"] == {"name": "translator", "found": False, "registered": False}
        assert record["template"] == {"name": "wiki-note", "found": False, "registered": False}

    def test_resolve_none_values(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        mgr = CapabilityManager(ops, skills)
        record = mgr.resolve(persona="none", operation="none", template="none")
        assert record == {"operation": None, "persona": None, "template": None}

    def test_resolve_unknown_operation(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        mgr = CapabilityManager(ops, skills)
        record = mgr.resolve(operation="ghost")
        assert record["operation"] == {"name": "ghost", "found": False}

    def test_validate_inputs_unknown(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        mgr = CapabilityManager(ops, skills)
        ok, missing = mgr.validate_inputs("ghost")
        assert ok is False
        assert "not found" in missing[0]

    def test_validate_inputs_stub_known(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        _write(ops / "synth.md", """
            ---
            type: operation
            expected_inputs:
              - part_digests
            ---
            body
        """)
        mgr = CapabilityManager(ops, skills)
        # Phase 4 stub: when available=None, just confirm capability exists.
        ok, missing = mgr.validate_inputs("synth")
        assert ok is True
        assert missing == []

    def test_validate_inputs_detects_missing_when_set_given(self, tmp_path):
        ops, skills = _make_dirs(tmp_path)
        _write(ops / "synth.md", """
            ---
            type: operation
            expected_inputs:
              - part_digests
              - title
            ---
            body
        """)
        mgr = CapabilityManager(ops, skills)
        ok, missing = mgr.validate_inputs("synth", available={"title"})
        assert ok is False
        assert missing == ["part_digests"]
        ok, missing = mgr.validate_inputs("synth", available={"part_digests", "title"})
        assert ok is True
        assert missing == []

# ── CapabilitySpec ──────────────────────────────────────────────────

class TestCapabilitySpec:
    def test_to_trace_record(self, tmp_path):
        spec = CapabilitySpec(
            name="foo",
            type="operation",
            source_path=tmp_path / "foo.md",
            description="x",
            produces=("out",),
            cost_class="low",
        )
        record = spec.to_trace_record()
        assert record["name"] == "foo"
        assert record["found"] is True
        assert record["cost_class"] == "low"
        assert record["produces"] == ["out"]
        assert record["source"].endswith("foo.md")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
