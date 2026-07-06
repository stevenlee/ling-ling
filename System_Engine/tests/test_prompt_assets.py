"""Prompt-asset lint: referential integrity of the vault prompt system.

The prompt system spans vault files (Scripture/Personas/Profiles/Templates/
Operations/Prompts) whose references are resolved at RUNTIME by name — a broken
reference (renamed persona, deleted template) only surfaces when that path is
exercised. These tests turn "no dead links today" into a standing guarantee:

  * Scripture.md defaults (`be_a`, `use_template`) resolve to real files.
  * Every Profile's `persona`/`template`/`operations` refs resolve (including
    the `_pending/` review queue — those go live by a move, so they must lint).
  * No orphan `.zh`/`.ja` language variant (base file gone → variant is dead).
  * The agent prompt files BaseAgent loads all exist (a missing file silently
    degrades to a hardcoded fallback or empty prompt — behavior drift, no error).
  * The mermaid math-policy sentinel: the vault's LLM-facing rules
    (Templates/Prompts/mermaid_rules.md) must carry the CURRENT policy marker,
    so it can't silently diverge from the deterministic repair pipeline again
    (it once banned all label math while the pipeline preserved clean KaTeX).

Skipped entirely when the vault isn't checked out next to System_Engine.
"""

import re

import pytest
import yaml

from core.config import (
    GUIDELINES_DIR,
    OPERATIONS_DIR,
    PERSONAS_DIR,
    PROFILES_DIR,
    PROMPTS_DIR,
    SCRIPTURE_FILE,
    TEMPLATES_DIR,
)

pytestmark = pytest.mark.skipif(
    not SCRIPTURE_FILE.exists(), reason="vault (lings-desktop) not present"
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _frontmatter(path) -> dict:
    m = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return {}
    data = yaml.safe_load(m.group(1))
    return data if isinstance(data, dict) else {}


def _profiles():
    """Every profile file, active AND `_pending/` (activation is just a move,
    so pending profiles must already lint). `_README.md` is documentation."""
    return [p for p in sorted(PROFILES_DIR.rglob("*.md")) if not p.name.startswith("_")]


class TestReferentialIntegrity:
    def test_scripture_defaults_resolve(self):
        fm = _frontmatter(SCRIPTURE_FILE)
        persona, template = fm.get("be_a"), fm.get("use_template")
        assert persona, "Scripture.md is missing `be_a`"
        assert (PERSONAS_DIR / f"{persona}.md").exists(), f"be_a → 缺 Personas/{persona}.md"
        assert template, "Scripture.md is missing `use_template`"
        assert (TEMPLATES_DIR / f"{template}.md").exists(), (
            f"use_template → 缺 Templates/{template}.md"
        )

    def test_profile_refs_resolve(self):
        broken: list[str] = []
        for prof in _profiles():
            fm = _frontmatter(prof)
            persona, template = fm.get("persona"), fm.get("template")
            if persona and not (PERSONAS_DIR / f"{persona}.md").exists():
                broken.append(f"{prof.name}: persona '{persona}'")
            if template and not (TEMPLATES_DIR / f"{template}.md").exists():
                broken.append(f"{prof.name}: template '{template}'")
            for op in fm.get("operations") or []:
                if not (OPERATIONS_DIR / f"{op}.md").exists():
                    broken.append(f"{prof.name}: operation '{op}'")
        assert not broken, f"Profile 引用斷鏈: {broken}"

    def test_profiles_exist_at_all(self):
        # An empty Profiles/ dir would make every ref-check vacuously pass.
        assert _profiles(), "Scripture/Profiles/ has no profile files"

    def test_no_orphan_language_variants(self):
        # `X.zh.md` / `X.ja.md` without a base `X.md` is dead localization:
        # load_localized_content() only looks for variants FROM the base name.
        orphans: list[str] = []
        for root in (PERSONAS_DIR, GUIDELINES_DIR, TEMPLATES_DIR):
            for variant in root.rglob("*.md"):
                m = re.match(r"^(?P<base>.+)\.(zh|ja)$", variant.stem)
                if m and not (variant.parent / f"{m.group('base')}.md").exists():
                    orphans.append(str(variant.relative_to(root.parent)))
        assert not orphans, f"孤兒語言變體(base 已不存在): {orphans}"

    def test_required_agent_prompts_exist(self):
        # BaseAgent._load_prompt silently returns "" (or a hardcoded fallback)
        # for a missing file — behavior drifts with no error. Fail loudly here.
        # Keep in sync with maintenance/health_check.py::required_prompts.
        required = [
            "system_base.md",
            "mermaid_rules.md",
            "agent_counter.md",
            "agent_insight.md",
            "agent_linter.md",
            "agent_merge.md",
            "agent_recall.md",
            "agent_tag_patrol.md",
        ]
        missing = [name for name in required if not (PROMPTS_DIR / name).exists()]
        assert not missing, f"Agent prompt 檔缺失: {missing}"


class TestMermaidPolicySentinel:
    """Guards `math-policy: katex-v2` — the vault's LLM-facing mermaid rules and
    the deterministic repair pipeline once contradicted each other (file said
    "no label math ever", pipeline preserved clean KaTeX). The marker ties them:
    changing the policy in code requires touching the file AND bumping the
    marker here, in mermaid_repair.py, and in the vault file together."""

    def _rules(self) -> str:
        return (PROMPTS_DIR / "mermaid_rules.md").read_text(encoding="utf-8")

    def test_policy_marker_is_current(self):
        assert "math-policy: katex-v2" in self._rules()

    def test_old_blanket_math_ban_is_gone(self):
        # The pre-katex rule heading that told the repair LLM to strip ALL math.
        assert "No LaTeX / Math in Labels" not in self._rules()

    def test_states_the_katex_form(self):
        text = self._rules()
        assert "$$" in text, "rules no longer mention the `$$...$$` KaTeX form"
        assert "sequenceDiagram" in text, "rules lost the per-kind (sequence) policy"
