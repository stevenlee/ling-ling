"""Prompt composition: persona × operation × template + localization (P2b).

Moved from services/llm_client.py. `lang_hint()` / `localized_suffix()` are
pure settings reads (module functions); PromptComposer carries the file cache
and capability manager needed to assemble a full system prompt.
"""

from __future__ import annotations

from pathlib import Path

from core.config import (
    GUIDELINES_DIR,
    OPERATIONS_DIR,
    PERSONAS_DIR,
    PROJECT_ROOT,
    TEMPLATES_DIR,
    settings,
)
from core.parsing.markdown_quality import strip_body_frontmatter

_PROJECT_IDENTITY_FILES = ("README.md", "SCHEMA.md")
_PROJECT_IDENTITY_TRUNCATE = 4000


def lang_hint() -> str:
    lang = settings.OUTPUT_LANGUAGE.lower()
    if "simplified" in lang or "簡體" in lang or "简体" in lang or "zh-cn" in lang:
        return "Simplified Chinese (zh-CN, 简体中文). MUST NOT use Traditional Chinese (繁體中文)."
    if "traditional" in lang or "繁體" in lang or "繁体" in lang or "zh-tw" in lang:
        return "Traditional Chinese (zh-TW, 繁體中文). MUST NOT use Simplified Chinese (简体中文)."
    if "chinese" in lang or "中文" in lang:
        return f"{settings.OUTPUT_LANGUAGE}. Please be consistent and do NOT mix Simplified and Traditional characters."
    if "japanese" in lang or "日本語" in lang:
        return "Japanese (日本語)"
    return settings.OUTPUT_LANGUAGE


def localized_suffix() -> str:
    lang = settings.OUTPUT_LANGUAGE.lower()
    if "chinese" in lang or "中文" in lang:
        return ".zh"
    if "japanese" in lang or "日本語" in lang:
        return ".ja"
    return ""


def language_banner() -> str:
    """The leading OUTPUT-LANGUAGE banner used by build_system_prompt().

    Extracted so the lean `complete()` path can opt in (P4): a caller that emits
    user-visible prose but bypasses PromptComposer has no language guarantee, so
    it can prepend this. Pins the response to OUTPUT_LANGUAGE — do NOT use it for
    calls whose language should follow the *content* (e.g. learning-aid artifacts
    keep an English note's diagram English) or for strict-JSON extraction."""
    hint = lang_hint()
    return (
        f"OUTPUT LANGUAGE (highest priority): write the ENTIRE response — every section "
        f"heading and all body text — in {hint}. Any English headings or labels in the "
        f"instructions/template below are illustrative only; translate them into {hint}, "
        f"never copy them verbatim."
    )


class PromptComposer:
    def __init__(self, file_cache, capability_manager):
        self._file_cache = file_cache
        self.capability_manager = capability_manager

    def load_localized_content(self, file_path: Path) -> str:
        suffix = localized_suffix()
        if suffix:
            localized = file_path.parent / f"{file_path.stem}{suffix}{file_path.suffix}"
            if localized.exists():
                return self._file_cache.read(localized)
        return self._file_cache.read(file_path)

    def load_capability_body(self, file_path: Path) -> str:
        """Load an Operation/Skill body for inclusion in a system prompt.

        Strips the YAML frontmatter (Phase 4 capability metadata) so it
        does not leak into the model's system prompt. Returns just the
        prompt body text.
        """
        raw = self.load_localized_content(file_path)
        if not raw:
            return ""
        body, _ = strip_body_frontmatter(raw)
        return body.strip()

    def load_project_identity(self) -> str:
        parts = []
        for filename in _PROJECT_IDENTITY_FILES:
            content = self._file_cache.read(PROJECT_ROOT / filename)
            if content:
                parts.append(content[:_PROJECT_IDENTITY_TRUNCATE])
        return "\n\n---\n\n".join(parts)

    def build_system_prompt(
        self,
        instruction_type: str,
        forced_template: str | None = None,
        default_template: str | None = None,
        require_yaml_header: bool = True,
        persona: str | None = None,
        operation: str | None = None,
    ) -> tuple[str, dict]:
        """Build the system prompt + capability resolution record.

        Returns `(prompt_text, resolution_dict)`. The resolution dict belongs
        in `trace_context["metadata"]["capability_resolution"]` — it is never
        injected into the prompt itself.
        """
        # Persona axis: None → settings.AGENT_ROLE (legacy), "none" → no persona,
        # any other string → load that persona file. Lets fixed-methodology
        # operations (Stitch / Synthesize) opt out of the global AGENT_ROLE.
        if persona == "none":
            role_instructions = ""
            persona_resolved = "none"
        else:
            persona_resolved = persona or settings.AGENT_ROLE
            # Loaded via load_capability_body: personas now share the unified
            # frontmatter contract (description/applicable_when), which must
            # not leak into the system prompt. Frontmatter-less files pass
            # through unchanged.
            role_instructions = self.load_capability_body(PERSONAS_DIR / f"{persona_resolved}.md")

        # Operation axis: a persona-agnostic methodology prompt (Synthesize,
        # Critique, ...). Orthogonal to Template (which controls output shape).
        # Loaded via load_capability_body so the Phase 4 capability frontmatter
        # is stripped before the body is concatenated into the system prompt.
        operation_instructions = ""
        if operation and operation != "none":
            operation_instructions = self.load_capability_body(OPERATIONS_DIR / f"{operation}.md")

        if forced_template == "none":
            template_instructions = ""
            template_resolved = "none"
        else:
            template_resolved = (
                (forced_template or default_template) or settings.USE_TEMPLATE or "wiki-note"
            )
            template_name = (
                template_resolved
                if template_resolved.endswith(".md")
                else f"{template_resolved}.md"
            )
            template_instructions = self.load_capability_body(TEMPLATES_DIR / template_name)

        viz_instructions = self.load_localized_content(GUIDELINES_DIR / "Visualization.md")

        hint = lang_hint()
        strict_hint = (
            "\n## STRICT ADHERENCE REQUIRED\n"
            "You MUST follow the provided Markdown template exactly. "
            "Do NOT add conversational fillers, greetings, or meta-comments. "
            "Focus exclusively on structured content."
            if settings.STRICT_MODE
            else ""
        )
        yaml_rule = (
            "Use the standard YAML header (--- title: ... ---) at the beginning of your response."
            if require_yaml_header
            else "Do not include YAML frontmatter unless the user explicitly asks for it."
        )
        # Leading language banner — first thing the model reads. Personas,
        # operations and several templates are English-only; without an explicit
        # override the model copies their English section headers verbatim and
        # the whole page can drift to English. Stated first AND restated last
        # (common_rules) so it survives the English bulk in the middle.
        lang_banner = language_banner()
        common_rules = (
            f"\n## Output Language\nOutput everything — including all section headings — in {hint}. "
            f"The section headers shown in the template are illustrative; render them in {hint}, "
            f"never reproduce them in English.{strict_hint}\n\n"
            f"## Task\n{instruction_type}\n\n{viz_instructions}\n\n{yaml_rule}"
        )
        sections = [
            s for s in (role_instructions, operation_instructions, template_instructions) if s
        ]
        sections.append(common_rules)
        prompt = lang_banner + "\n\n" + "\n\n".join(sections)

        resolution = self.capability_manager.resolve(
            persona=persona_resolved,
            operation=operation,
            template=template_resolved,
        )
        return prompt, resolution
