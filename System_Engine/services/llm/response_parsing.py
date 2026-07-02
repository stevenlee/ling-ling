"""Wiki-note response parsing: YAML+Markdown hybrid + frontmatter cleanup (P2b).

Moved verbatim from services/llm_client.py staticmethods.
"""

from __future__ import annotations

import logging
import re

import yaml

from core.parsing.markdown_quality import clean_llm_response, strip_body_frontmatter

_FENCED_MARKDOWN_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n```$", re.DOTALL | re.IGNORECASE)
_YAML_HEADER_RE = re.compile(
    r"(?:^|\n)(?:---|```yaml)\s*\n(.*?)\n(?:---|```)\s*(?:\n|$)", re.DOTALL
)
_YAML_MARKDOWN_CLEANUP_RE = re.compile(
    r"(^|[:\[,\s])[\*\_]{1,2}(.*?)[\*\_]{1,2}(?=[\]\s,:]|$)",
    re.MULTILINE,
)
_H1_TITLE_RE = re.compile(r"^#\s+(.*)", re.MULTILINE)


def strip_accidental_frontmatter(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = _FENCED_MARKDOWN_RE.sub(r"\1", text).strip()
    text, _ = strip_body_frontmatter(text)
    return text.strip()


def hybrid_parse(text: str) -> dict:
    """Find the Wiki Note YAML+body inside a model response."""
    if not text:
        return {"title": "Untitled", "tags": [], "type": "entity", "content": ""}

    text = clean_llm_response(text)
    result = {"title": "Untitled", "tags": [], "type": "entity", "content": text}

    yaml_match = _YAML_HEADER_RE.search(text)
    if yaml_match:
        yaml_str = yaml_match.group(1).strip()
        try:
            # Bold/italic markers can sneak into LLM-produced YAML.
            clean_yaml_str = _YAML_MARKDOWN_CLEANUP_RE.sub(r'\1"\2"', yaml_str)
            metadata = yaml.safe_load(clean_yaml_str)
        except Exception as e:
            if "```yaml" in yaml_match.group(0):
                logging.warning(f"YAML parse failed: {e}\nOffending string:\n{yaml_str}")
            metadata = None

        if isinstance(metadata, dict):
            for key in ("title", "tags", "type", "pending_concepts"):
                if key in metadata:
                    result[key] = str(metadata[key]) if key == "title" else metadata[key]
            result["content"] = clean_llm_response(text[yaml_match.end() :].strip())
            return result

    title_match = _H1_TITLE_RE.search(text)
    if title_match:
        result["title"] = title_match.group(1).strip()
    return result
