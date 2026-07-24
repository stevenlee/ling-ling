"""Deterministic publication gate for generated entity-page bodies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_REASONING_LABEL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:[*_`]\s*){0,2}(?:#{0,3}\s*)?"
    r"(?:Source Material|Input|Content|Goal|Constraints|Wait|"
    r"Final check|Self-Correction|Check)\s*[:：]",
    re.IGNORECASE | re.MULTILINE,
)
_REPEATED_CHAR_RE = re.compile(r"([^\s])\1{39,}")
_HTML_HEADING_RE = re.compile(r"</?h[1-6](?:\s[^>]*)?>", re.IGNORECASE)
_MIXED_MERMAID_ID_RE = re.compile(r"(?m)^\s*[A-Za-z]+[\u3400-\u9fff]+[A-Za-z\u3400-\u9fff]*\s*\[")


@dataclass(frozen=True)
class EntityQualityResult:
    hard_issues: list[str] = field(default_factory=list)
    suspect_issues: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.hard_issues and not self.suspect_issues


def assess_entity_body(body: str) -> EntityQualityResult:
    hard: list[str] = []
    suspect: list[str] = []
    stripped = (body or "").strip()
    if not stripped:
        hard.append("empty_body")
        return EntityQualityResult(hard, suspect)
    if "```yaml" in stripped.lower():
        hard.append("yaml_contract_leaked_into_body")
    if len(_REASONING_LABEL_RE.findall(stripped)) >= 3:
        hard.append("reasoning_or_prompt_leakage")
    if _REPEATED_CHAR_RE.search(stripped):
        hard.append("repeated_character_truncation")
    if stripped.count("```") % 2:
        hard.append("unclosed_code_fence")
    prose = re.sub(r"```.*?```", "", stripped, flags=re.DOTALL)
    prose = re.sub(r"`[^`\n]+`", "", prose)
    if _HTML_HEADING_RE.search(prose):
        suspect.append("embedded_html_heading")
    if "```mermaid" in stripped.lower() and _MIXED_MERMAID_ID_RE.search(stripped):
        suspect.append("corrupted_mermaid_identifier")
    return EntityQualityResult(hard, suspect)
