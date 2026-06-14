"""LLM provider client.

Public API (used across agents/, services/, watchers/, maintenance/):

    LLMClient()
        .answer_query(query_content, wiki_context, ...) -> str
        .generate_entity_page(markdown_content=..., filename=..., ...) -> dict | None
        .translate_tags(tags) -> dict
        .generate_part_digest(title, part_number, total_parts, raw_chunk, ...) -> dict
        .generate_synthesis(title, part_digests, final_concepts) -> str
        .critique_text(candidate, sources, focus=None) -> str
        .score_text_quality(text, prompt_version="v1") -> dict   # P0 (thoughtful splitter)

This module is the single seam between the agent layer and the upstream LLM
provider (vllm / gemini / ollama). All provider-specific branching is
contained here so the agents stay provider-agnostic.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from core.config import (
    GUIDELINES_DIR,
    LLM_PROVIDER,
    OPERATIONS_DIR,
    PERSONAS_DIR,
    PROJECT_ROOT,
    SKILLS_DIR,
    TEMPLATES_DIR,
    settings,
)
from core.parser import extract_json_array, extract_json_object, is_empty_json_literal, strip_body_frontmatter, clean_llm_response
from core.utils import MtimeCache, digest_value_to_text
from services.capability_manager import CapabilityManager
from services.trace_store import TraceStore, elapsed_ms, usage_to_counts


# ─── Constants ────────────────────────────────────────────────────────

_OPENAI_COMPATIBLE_PROVIDERS = frozenset({"vllm", "ollama"})

_LANG_HINT_MAP = {
    "Traditional Chinese (繁體中文)": {"file": "檔案名稱", "content": "素材內容"},
    "Japanese (日本語)":              {"file": "ファイル名", "content": "素材内容"},
}
_DEFAULT_LABELS = {"file": "Filename", "content": "Content"}

_FENCED_MARKDOWN_RE = re.compile(r'^```(?:markdown|md)?\s*\n(.*?)\n```$', re.DOTALL | re.IGNORECASE)
_YAML_HEADER_RE = re.compile(r'(?:^|\n)(?:---|```yaml)\s*\n(.*?)\n(?:---|```)\s*(?:\n|$)', re.DOTALL)
_YAML_MARKDOWN_CLEANUP_RE = re.compile(
    r'(^|[:\[,\s])[\*\_]{1,2}(.*?)[\*\_]{1,2}(?=[\]\s,:]|$)',
    re.MULTILINE,
)
_H1_TITLE_RE = re.compile(r'^#\s+(.*)', re.MULTILINE)

_PROJECT_IDENTITY_FILES = ("README.md", "SCHEMA.md")
_PROJECT_IDENTITY_TRUNCATE = 4000


# ─── Quality scorer prompts (version-locked) ──────────────────────────
#
# Prompt text is locked in code so regression runs across days/weeks see the
# same prompt. To change a prompt, add a new version key — never edit an
# existing version after it's been used to produce baseline scores.

_CHUNK_COHERENCE_PROMPTS: dict[str, str] = {
    "v1": (
        "You are evaluating how self-contained a text chunk is for use as a "
        "retrieval unit in a knowledge base. Score 1-10:\n"
        "- 10: Reads as a complete, standalone thought. No dangling references.\n"
        "- 7-9: Mostly self-contained, minor context-dependence.\n"
        "- 4-6: Somewhat broken at the start or end mid-thought.\n"
        "- 1-3: Severely fragmented; cannot stand alone.\n\n"
        "Return ONLY a JSON object with this exact schema:\n"
        '{"score": <integer 1-10>, "reason": "<one short sentence>"}\n\n'
        "Do not include any text outside the JSON object."
    ),
}

# Topic-shift detection (Phase 4 of the Thoughtful Splitter).
#
# LLMs hallucinate character offsets but reliably handle PARAGRAPH INDICES
# — so the contract is "split_after paragraph N", not "split at offset N".
# The splitter converts indices back to source offsets deterministically.
#
# IMPORTANT: never edit an existing version after baseline outputs exist.
# To change a prompt, add a new version key.

# Context summary (Phase 5 of the Thoughtful Splitter).
#
# A 1-2 sentence factual summary of the previous chunk, used as a context
# preamble for the next chunk. Replaces structural overlap when enabled.
# Must match the source language (Chinese in, Chinese out).
#
# Same versioning rules: never edit an existing version.

_SUMMARY_PROMPTS: dict[str, str] = {
    "v1": (
        "You are generating a brief context preamble. A reader is about to read a section\n"
        "of text and you must hand them the gist of the section that came IMMEDIATELY before,\n"
        "so they can pick up the thread.\n\n"
        "Rules:\n"
        "- 1 to 2 sentences, total length ≤ 200 characters.\n"
        "- Match the INPUT LANGUAGE exactly (Chinese in → Chinese out; English in → English out).\n"
        "- Write declarative facts. No \"As we saw...\", \"This passage discusses...\", or other meta framing.\n"
        "- Preserve key proper nouns, names, terms, and the conclusion.\n"
        "- Do not invent facts that the input doesn't support.\n\n"
        "Return ONLY a JSON object with this exact schema:\n"
        '  {"summary": "<1-2 sentences, ≤ 200 chars>"}\n\n'
        "No prose, no markdown, no commentary outside the JSON."
    ),
}


_TOPIC_SHIFT_PROMPTS: dict[str, str] = {
    "v1": (
        "You are an editor segmenting a long passage of prose into self-contained sections.\n"
        "The passage has no chapter headings — it flows through one or more topics paragraph by paragraph.\n\n"
        "You will receive numbered paragraphs. Identify 0, 1, or 2 paragraph boundaries where the topic\n"
        "**clearly shifts** to a substantially new idea (not just a sub-point, restatement, or example).\n\n"
        "Return ONLY a single JSON object with this exact schema:\n"
        '  {"split_after": [<paragraph_number>, ...]}\n\n'
        "Examples:\n"
        '  - All paragraphs continue one topic → {"split_after": []}\n'
        '  - Paragraphs 1-3 about topic A, 4-N about topic B → {"split_after": [3]}\n'
        '  - Three distinct topics with shifts after P3 and P6 → {"split_after": [3, 6]}\n\n'
        "Rules:\n"
        "- Each value must be between 1 and (N-1) inclusive. You cannot split before the first\n"
        "  paragraph or after the last.\n"
        "- Maximum 2 entries. If you detect more than 2 shifts, return only the 2 strongest.\n"
        "- Only a **clear topic shift** counts — a paragraph that opens a substantially new line\n"
        "  of thought, not one that elaborates or rephrases the previous one.\n"
        "- Output ONLY the JSON object. No prose, no markdown, no commentary."
    ),
}


# ─── Lazy provider SDK imports ────────────────────────────────────────

_GENAI_MOD = None


def _genai():
    """Lazy-load google.genai (only when gemini provider is active)."""
    global _GENAI_MOD
    if _GENAI_MOD is None:
        from google import genai as _g
        _GENAI_MOD = _g
    return _GENAI_MOD


_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


def _error_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                pass
    return None


def _is_non_retryable_llm_error(exc: Exception) -> bool:
    code = _error_status_code(exc)
    if code in _NON_RETRYABLE_STATUS_CODES:
        return True

    cls_name = type(exc).__name__
    non_retry_keywords = (
        "Authentication",
        "Permission",
        "BadRequest",
        "InvalidArgument",
        "NotFound",
    )
    if any(kw in cls_name for kw in non_retry_keywords):
        return True

    return False


def _is_transient_llm_error(exc: Exception) -> bool:
    if _is_non_retryable_llm_error(exc):
        return False

    code = _error_status_code(exc)
    if code in _TRANSIENT_STATUS_CODES:
        return True

    cls_name = type(exc).__name__
    transient_keywords = (
        "RateLimit",
        "Timeout",
        "Connection",
        "APIConnection",
        "ServiceUnavailable",
    )
    if any(kw in cls_name for kw in transient_keywords):
        return True

    err_msg = str(exc).lower()
    fallback_keywords = (
        "timeout",
        "temporarily unavailable",
        "connection",
        "rate limit",
        "too many requests",
    )
    if any(kw in err_msg for kw in fallback_keywords):
        return True

    return False


# ─── Client ────────────────────────────────────────────────────────────

class LLMClient:
    def __init__(self):
        self.provider = LLM_PROVIDER
        self._file_cache = MtimeCache()
        self.trace_store = TraceStore()
        self.capability_manager = CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)

        if self.provider == "vllm":
            self.client, self.model = self._build_openai_client(
                base_url=os.getenv("VLLM_API_BASE", "http://192.168.1.103:9000/v1"),
                api_key=os.getenv("VLLM_API_KEY", "dummy-token"),
                model=os.getenv("VLLM_MODEL", "gpt-oss-20b"),
            )
        elif self.provider == "ollama":
            self.client, self.model = self._build_openai_client(
                base_url=os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
                api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
                model=os.getenv("OLLAMA_MODEL", "gemma2:27b"),
            )
        elif self.provider == "gemini":
            self.client = _genai().Client(api_key=os.getenv("GEMINI_API_KEY"))
            self.model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER {self.provider!r}. "
                f"Expected one of: vllm, gemini, ollama."
            )

    @staticmethod
    def _build_openai_client(*, base_url: str, api_key: str, model: str):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("pip install openai") from e
        return OpenAI(base_url=base_url, api_key=api_key, timeout=300.0), model

    # ─── Provider dispatch ──────────────────────────────────────────────

    def _complete_text(
        self,
        system_prompt: str,
        user_msg: Any,
        temperature: float | None = None,
        max_tokens: int | None = None,
        trace_context: dict | None = None,
    ) -> str:
        temperature = settings.CREATIVITY if temperature is None else temperature
        max_tokens = settings.MAX_OUTPUT if max_tokens is None else max_tokens
        trace_context = dict(trace_context or {})
        started = time.perf_counter()

        retry_meta = {
            "retry_attempts": 0,
            "retry_transient": False,
        }

        try:
            text, prompt_tokens, completion_tokens, total_tokens = (
                self._complete_provider_text_with_retry(
                    system_prompt,
                    user_msg,
                    temperature,
                    max_tokens,
                    retry_meta=retry_meta,
                )
            )

            try:
                metadata = {
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **trace_context.pop("metadata", {}),
                    **retry_meta,
                }
                self.trace_store.record_llm_call(
                    system_prompt=system_prompt,
                    user_msg=user_msg,
                    response_text=text,
                    provider=self.provider,
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=elapsed_ms(started),
                    status="succeeded",
                    metadata=metadata,
                    **trace_context,
                )
            except Exception as trace_error:
                logging.debug(f"LLM trace write failed: {trace_error}")
            return text
        except Exception as e:
            try:
                metadata = {
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    **trace_context.pop("metadata", {}),
                    **retry_meta,
                }
                self.trace_store.record_llm_call(
                    system_prompt=system_prompt,
                    user_msg=user_msg,
                    response_text=None,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=elapsed_ms(started),
                    status="failed",
                    error=str(e),
                    metadata=metadata,
                    **trace_context,
                )
            except Exception as trace_error:
                logging.debug(f"LLM trace write failed: {trace_error}")
            raise

    def _complete_provider_text_once(
        self,
        system_prompt: str,
        user_msg: Any,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, int | None, int | None, int | None]:
        if self.provider == "gemini":
            genai = _genai()
            response = self.client.models.generate_content(
                model=self.model,
                contents=user_msg if isinstance(user_msg, list) else [str(user_msg)],
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            text = response.text or ""
            prompt_tokens, completion_tokens, total_tokens = self._gemini_usage_counts(response)
        else:
            text, usage = self._openai_chat(system_prompt, user_msg, temperature, max_tokens)
            prompt_tokens, completion_tokens, total_tokens = usage_to_counts(usage)
        return text, prompt_tokens, completion_tokens, total_tokens

    def _complete_provider_text_with_retry(
        self,
        system_prompt: str,
        user_msg: Any,
        temperature: float,
        max_tokens: int,
        *,
        retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        retry_meta: dict,
    ) -> tuple[str, int | None, int | None, int | None]:
        import random
        attempts = 0
        last_error = None

        while attempts < retries:
            attempts += 1
            retry_meta["retry_attempts"] = attempts
            try:
                return self._complete_provider_text_once(
                    system_prompt, user_msg, temperature, max_tokens
                )
            except Exception as e:
                last_error = e
                retry_meta["retry_last_error"] = str(e)
                if _is_transient_llm_error(e):
                    retry_meta["retry_transient"] = True
                    if attempts < retries:
                        delay = initial_delay * (backoff_factor ** (attempts - 1))
                        jitter = random.uniform(0, 0.2 * delay)
                        total_delay = delay + jitter
                        logging.warning(
                            f"LLM provider call failed transiently (attempt {attempts}/{retries}): {e}. "
                            f"Retrying in {total_delay:.2f} seconds..."
                        )
                        time.sleep(total_delay)
                        continue
                raise e

    @staticmethod
    def _gemini_usage_counts(response: Any) -> tuple[int | None, int | None, int | None]:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None, None, None
        prompt = getattr(usage, "prompt_token_count", None)
        completion = getattr(usage, "candidates_token_count", None)
        total = getattr(usage, "total_token_count", None)
        return prompt, completion, total

    def _openai_chat(self, system_prompt: str, user_msg: Any, temperature: float, max_tokens: int) -> tuple[str, Any]:
        extra_body = {"num_ctx": settings.MEMORY_LIMIT} if self.provider == "ollama" else {}
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        message = response.choices[0].message
        content = message.content or ""
        if not content.strip():
            # Reasoning models served via Ollama (e.g. gemma thinking
            # variants) intermittently emit the whole answer — including
            # the final JSON — into the reasoning channel and leave
            # content empty. Fall back so scanning parsers
            # (extract_json_*, verdict regexes) can still find the answer;
            # an empty string is strictly worse for every caller.
            reasoning = getattr(message, "reasoning", None) or getattr(
                message, "reasoning_content", None
            )
            if isinstance(reasoning, str) and reasoning.strip():
                logging.warning(
                    "LLM returned empty content with non-empty reasoning; "
                    "falling back to the reasoning channel."
                )
                content = reasoning
        return content, getattr(response, "usage", None)

    def trace_run(self, **kwargs):
        return self.trace_store.run(**kwargs)

    def current_trace_ids(self) -> list[str]:
        return self.trace_store.current_trace_ids()

    def current_run_id(self) -> str | None:
        return self.trace_store.current_run_id()

    # ─── Localized prompt loading ───────────────────────────────────────

    def _get_lang_hint(self) -> str:
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

    def _localized_suffix(self) -> str:
        lang = settings.OUTPUT_LANGUAGE.lower()
        if "chinese" in lang or "中文" in lang:
            return ".zh"
        if "japanese" in lang or "日本語" in lang:
            return ".ja"
        return ""

    def _load_localized_content(self, file_path: Path) -> str:
        suffix = self._localized_suffix()
        if suffix:
            localized = file_path.parent / f"{file_path.stem}{suffix}{file_path.suffix}"
            if localized.exists():
                return self._file_cache.read(localized)
        return self._file_cache.read(file_path)

    def _load_capability_body(self, file_path: Path) -> str:
        """Load an Operation/Skill body for inclusion in a system prompt.

        Strips the YAML frontmatter (Phase 4 capability metadata) so it
        does not leak into the model's system prompt. Returns just the
        prompt body text.
        """
        raw = self._load_localized_content(file_path)
        if not raw:
            return ""
        body, _ = strip_body_frontmatter(raw)
        return body.strip()

    def _load_project_identity(self) -> str:
        parts = []
        for filename in _PROJECT_IDENTITY_FILES:
            content = self._file_cache.read(PROJECT_ROOT / filename)
            if content:
                parts.append(content[:_PROJECT_IDENTITY_TRUNCATE])
        return "\n\n---\n\n".join(parts)

    def _build_system_prompt(
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
            # Loaded via _load_capability_body: personas now share the unified
            # frontmatter contract (description/applicable_when), which must
            # not leak into the system prompt. Frontmatter-less files pass
            # through unchanged.
            role_instructions = self._load_capability_body(PERSONAS_DIR / f"{persona_resolved}.md")

        # Operation axis: a persona-agnostic methodology prompt (Synthesize,
        # Critique, ...). Orthogonal to Template (which controls output shape).
        # Loaded via _load_capability_body so the Phase 4 capability frontmatter
        # is stripped before the body is concatenated into the system prompt.
        operation_instructions = ""
        if operation and operation != "none":
            operation_instructions = self._load_capability_body(OPERATIONS_DIR / f"{operation}.md")

        if forced_template == "none":
            template_instructions = ""
            template_resolved = "none"
        else:
            template_resolved = (forced_template or default_template) or settings.USE_TEMPLATE or "wiki-note"
            template_name = template_resolved if template_resolved.endswith(".md") else f"{template_resolved}.md"
            template_instructions = self._load_capability_body(TEMPLATES_DIR / template_name)

        viz_instructions = self._load_localized_content(GUIDELINES_DIR / "Visualization.md")

        lang_hint = self._get_lang_hint()
        strict_hint = (
            "\n## STRICT ADHERENCE REQUIRED\n"
            "You MUST follow the provided Markdown template exactly. "
            "Do NOT add conversational fillers, greetings, or meta-comments. "
            "Focus exclusively on structured content."
            if settings.STRICT_MODE else ""
        )
        yaml_rule = (
            "Use the standard YAML header (--- title: ... ---) at the beginning of your response."
            if require_yaml_header
            else "Do not include YAML frontmatter unless the user explicitly asks for it."
        )
        common_rules = (
            f"\n## Output Language\nPlease output everything in {lang_hint}.{strict_hint}\n\n"
            f"## Task\n{instruction_type}\n\n{viz_instructions}\n\n{yaml_rule}"
        )
        sections = [s for s in (role_instructions, operation_instructions, template_instructions) if s]
        sections.append(common_rules)
        prompt = "\n\n".join(sections)

        resolution = self.capability_manager.resolve(
            persona=persona_resolved,
            operation=operation,
            template=template_resolved,
        )
        return prompt, resolution

    # ─── Output cleanup ─────────────────────────────────────────────────

    @staticmethod
    def _strip_accidental_frontmatter(text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        text = _FENCED_MARKDOWN_RE.sub(r'\1', text).strip()
        text, _ = strip_body_frontmatter(text)
        return text.strip()

    @staticmethod
    def _hybrid_parse(text: str) -> dict:
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
                result["content"] = clean_llm_response(text[yaml_match.end():].strip())
                return result

        title_match = _H1_TITLE_RE.search(text)
        if title_match:
            result["title"] = title_match.group(1).strip()
        return result

    # ─── Public API ─────────────────────────────────────────────────────

    def generate_entity_page(
        self,
        markdown_content: str | None = None,
        filename: str | None = None,
        index_content: str = "",
        image_path: Path | None = None,
        context_hint: str | None = None,
        persona: str | None = None,
        forced_template: str | None = None,
    ) -> dict | None:
        instruction_type = "Convert this material into a structured Wiki entity page."
        system_prompt, cap_resolution = self._build_system_prompt(
            instruction_type,
            persona=persona,
            forced_template=forced_template,
        )
        labels = _LANG_HINT_MAP.get(self._get_lang_hint(), _DEFAULT_LABELS)

        try:
            if image_path:
                user_msg = self._build_multimodal_user_msg(image_path, filename, labels)
                response = self._complete_text(
                    system_prompt,
                    user_msg,
                    trace_context={
                        "stage": "generate_entity_page",
                        "metadata": {
                            "filename": filename,
                            "input_kind": "image",
                            "capability_resolution": cap_resolution,
                        },
                    },
                )
            else:
                user_text = f"{labels['file']}: {filename}\n\n"
                if context_hint:
                    user_text += f"[Context]: {context_hint}\n\n"
                user_text += f"{labels['content']}:\n{markdown_content}"
                response = self._complete_text(
                    system_prompt,
                    user_text,
                    trace_context={
                        "stage": "generate_entity_page",
                        "metadata": {
                            "filename": filename,
                            "input_kind": "markdown",
                            "capability_resolution": cap_resolution,
                        },
                    },
                )
            return self._hybrid_parse(response)
        except Exception as e:
            logging.error(f"LLM Error in generate_entity_page: {e}")
            return None

    def _build_multimodal_user_msg(self, image_path: Path, filename: str | None, labels: dict) -> Any:
        mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        raw_bytes = Path(image_path).read_bytes()

        if self.provider == "gemini":
            genai = _genai()
            return [
                f"{labels['file']}: {filename}",
                genai.types.Part.from_bytes(data=raw_bytes, mime_type=mime_type),
            ]
        encoded = base64.b64encode(raw_bytes).decode("utf-8")
        return [
            {"type": "text", "text": f"{labels['file']}: {filename}"},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
        ]

    def answer_query(
        self,
        query_content: str,
        wiki_context: str,
        custom_instruction: str | None = None,
        temperature: float | None = None,
        forced_template: str | None = None,
        default_template: str | None = None,
        persona: str | None = None,
        operation: str | None = None,
    ) -> str:
        cap_resolution: dict | None = None
        template_requested = forced_template or default_template
        use_template_builder = bool(custom_instruction) or (
            bool(template_requested) and template_requested != "none"
        )
        if use_template_builder:
            # Two callers land here:
            #   1. custom_instruction set (digest/critique helpers) — legacy
            #      behavior: the instruction IS the task, no YAML header forced.
            #   2. a /template selected on the default Q&A path with no custom
            #      instruction — honor the template's own YAML schema so the
            #      output is a clean template-shaped document.
            instruction = custom_instruction or (
                "Answer the user's request by producing a document that strictly "
                "follows the provided Markdown template. Draw the content from the "
                "provided source text; do not add conversational framing."
            )
            system_prompt, cap_resolution = self._build_system_prompt(
                instruction,
                forced_template=forced_template,
                default_template=default_template,
                require_yaml_header=not custom_instruction,
                persona=persona,
                operation=operation,
            )
            ctx = wiki_context if wiki_context.strip() else "(No relevant context retrieved.)"
            user_msg = (
                f"## User Directive\n{query_content}\n\n"
                f"## Provided Source Text\n{ctx}\n"
            )
        else:
            lang_hint = self._get_lang_hint()
            system_prompt = (
                "You are Ling-Ling's question-answering interface.\n\n"
                f"Answer the user's question directly in {lang_hint}.\n"
                "Use the provided knowledge context only as reference material.\n"
                "Do not rewrite, summarize, or continue the context unless the user explicitly asks for that.\n"
                "If the context is irrelevant or insufficient, say so briefly and answer from the project identity information.\n"
                "When the user asks what Ling-Ling is, describe Ling-Ling as an Obsidian-vault-based agentic RAG knowledge system "
                "driven by Scripture, Skills, and Templates.\n"
                "Do not include YAML frontmatter.\n"
            )
            identity = self._load_project_identity() or "(No project identity available.)"
            ctx = wiki_context if wiki_context.strip() else "(No relevant context retrieved.)"
            user_msg = (
                f"## User Question\n{query_content}\n\n"
                f"## Project Identity\n{identity}\n\n"
                f"## Retrieved Knowledge Context\n{ctx}\n"
            )

        try:
            return self._complete_text(
                system_prompt,
                user_msg,
                temperature=temperature,
                trace_context={
                    "stage": "answer_query",
                    "persona": persona,
                    "operation": operation,
                    "template": forced_template or default_template,
                    "metadata": {
                        "custom_instruction": bool(custom_instruction),
                        "capability_resolution": cap_resolution,
                    },
                },
            )
        except Exception as e:
            logging.error(f"LLM Error in answer_query: {e}")
            return f"Error: {e}"

    def complete(
        self,
        system_prompt: str,
        user_msg: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stage: str = "complete",
    ) -> str:
        """Lean completion with a caller-supplied system prompt.

        Unlike answer_query, this injects NO persona / template / visualization
        scaffolding — the system prompt is used verbatim. For controlled tasks
        (e.g. Cortex recall's select-and-summarize) that must not inherit the
        Q&A document machinery (which made the model chase a Mermaid diagram and
        dump its scratchpad). Returns "" on failure (fail-open)."""
        try:
            return self._complete_text(
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=temperature,
                max_tokens=max_tokens,
                trace_context={"stage": stage, "metadata": {}},
            )
        except Exception as e:
            logging.error(f"LLM Error in complete ({stage}): {e}")
            return ""

    def _complete_json(self, *, kind: str = "object", **complete_kwargs):
        """Complete a JSON-output prompt with one reasoning-channel re-roll.

        Transport-level retries already live in `_complete_text`. This guards a
        different, silent failure: reasoning models (gemma via Ollama) sometimes
        emit the whole reply into the reasoning channel and leave the content
        with no parseable JSON — no exception is raised. One re-roll usually
        lands it. (Same failure that silently zeroed LingLens extraction; see
        roadmap R4.)

        `kind` is "array" or "object". Returns the parsed list/dict, or the
        empty container after a second miss so callers keep their fail-open
        paths. A genuinely empty answer — the model emitted a literal [] / {} —
        is returned WITHOUT a re-roll, so a real zero is never mistaken for a
        parse miss (mirrors the LingLens extraction rule). Per-attempt
        exceptions are caught and re-rolled.

        Callers must pass system_prompt/user_msg as keywords. Bespoke retry
        logic with stricter accept conditions (e.g. `_assess_falsifiability_once`,
        which re-rolls until the score itself parses) stays as-is.
        """
        parse = extract_json_array if kind == "array" else extract_json_object
        empty = [] if kind == "array" else {}
        base_trace = dict(complete_kwargs.pop("trace_context", {}) or {})
        base_meta = dict(base_trace.get("metadata") or {})
        for attempt in range(2):
            trace = {**base_trace, "metadata": {**base_meta, "json_attempt": attempt + 1}}
            try:
                raw = self._complete_text(trace_context=trace, **complete_kwargs)
            except Exception as e:
                logging.warning(f"_complete_json({kind}) call failed (attempt {attempt + 1}/2): {e}")
                continue
            parsed = parse(raw)
            if parsed:
                return parsed
            if is_empty_json_literal(raw, kind):
                return empty  # whole reply IS [] / {} — genuine empty, not a parse miss
            logging.warning(f"_complete_json({kind}) parse miss (attempt {attempt + 1}/2)")
        return empty

    def translate_tags(self, tags: list[str]) -> dict:
        """Map each tag to its English equivalent, as a JSON object.

        Routes through `_complete_json`, so it inherits transport-level retry
        (transient 429/503 are retried instead of silently returning {}) and
        the centralized trace/token accounting — rather than re-implementing
        provider dispatch and a ~35-line trace block by hand (audit C1).
        Fail-open: a parse miss after the re-roll yields {}.
        """
        return self._complete_json(
            kind="object",
            system_prompt="Return a JSON mapping of {original_tag: english_equivalent} for these tags.",
            user_msg=f"Tags: {tags}",
            temperature=0.1,
            trace_context={"stage": "translate_tags", "metadata": {"tag_count": len(tags)}},
        )

    # ── Phase 0.3.1: Source Digest ────────────────────────────────────

    def digest_sources(
        self,
        *,
        query: str,
        source_title: str,
        source_text: str,
        budget: int = 6000,
    ) -> str:
        """Compress a single source into a concise digest guided by a query.

        Unlike `generate_part_digest` (which processes one chunk of an
        ingestion pipeline), this method produces a cross-section digest of
        an entire source for multi-source Insight answers.
        """
        return self.answer_query(
            query_content=(
                f"Compress the following source into a concise digest of "
                f"approximately {budget} characters.\n\n"
                f"User directive: {query}\n\n"
                f"Source title: {source_title}\n\n"
                f"Source text:\n{source_text}"
            ),
            wiki_context="",
            custom_instruction=(
                "You are a source digest operator. Compress the source text "
                "while preserving: core thesis, evidence snippets, terms and "
                "motifs relevant to the user directive, and contrasts with "
                "other potential sources. Flag coverage warnings if the "
                "source appears truncated or incomplete. Output concise "
                "Markdown. Do not add YAML frontmatter."
            ),
            forced_template="none",
            persona="none",
            operation="digest_sources",
        )

    def generate_part_digest(
        self,
        title: str,
        part_number: int,
        total_parts: int,
        raw_chunk: str,
        part_note: str,
        pending_concepts: str = "",
    ) -> dict:
        """Create a structured map digest for one part of a long document."""
        lang_hint = self._get_lang_hint()
        system_prompt = (
            "You create compact, evidence-aware map digests for a later synthesis pass.\n"
            f"Output language: {lang_hint}.\n"
            "Return JSON only. No Markdown, no YAML, no commentary.\n"
        )
        prompt = (
            f"Document title: {title}\n"
            f"Part: {part_number}/{total_parts}\n\n"
            "Prior unresolved concepts:\n"
            f"{pending_concepts or '(none)'}\n\n"
            "Raw source chunk:\n"
            f"{raw_chunk}\n\n"
            "Generated part note:\n"
            f"{part_note}\n\n"
            "Return one JSON object with this schema:\n"
            "{\n"
            f'  "part": {part_number},\n'
            '  "title": "short part title",\n'
            '  "thesis": "the central claim or function of this part",\n'
            '  "key_points": ["3-6 concrete points, preserving names, mechanisms, and distinctions"],\n'
            '  "evidence": ["2-5 source-grounded details, examples, quotes, terms, or data points"],\n'
            '  "terms": ["important proper nouns or technical terms"],\n'
            '  "open_questions": ["ambiguities, missing context, contradictions, or follow-up questions"],\n'
            '  "handoff": "what the next or final synthesis must remember"\n'
            "}\n\n"
            "Rules:\n"
            "- Prefer specific details over generic summary language.\n"
            "- Do not invent facts that are not supported by the source chunk or generated note.\n"
            "- Keep each list item concise but information-rich.\n"
        )

        try:
            parsed = self._complete_json(
                kind="object",
                system_prompt=system_prompt,
                user_msg=prompt,
                temperature=0.2,
                max_tokens=1800,
                trace_context={
                    "stage": "generate_part_digest",
                    "metadata": {
                        "title": title,
                        "part_number": part_number,
                        "total_parts": total_parts,
                        # Inline system prompt — bypasses the three-axis
                        # (persona / operation / template) build, so no
                        # capability resolution applies. Recorded as None so
                        # downstream trace queries can rely on the key existing.
                        "capability_resolution": None,
                    },
                },
            )
            if parsed:
                return self._apply_part_digest_defaults(parsed, part_number)
        except Exception as e:
            logging.error(f"Part digest generation failed for {title} part {part_number}: {e}")

        return self._part_digest_fallback(title, part_number, part_note, pending_concepts)

    @staticmethod
    def _apply_part_digest_defaults(parsed: dict, part_number: int) -> dict:
        parsed.setdefault("part", part_number)
        parsed.setdefault("title", f"Part {part_number}")
        parsed.setdefault("thesis", "")
        parsed.setdefault("key_points", [])
        parsed.setdefault("evidence", [])
        parsed.setdefault("terms", [])
        parsed.setdefault("open_questions", [])
        parsed.setdefault("handoff", "")
        return parsed

    def _part_digest_fallback(self, title: str, part_number: int, part_note: str, pending_concepts: str) -> dict:
        cleaned = self._strip_accidental_frontmatter(part_note).strip().splitlines()
        lines = [line.strip("#- * \t") for line in cleaned if line.strip()][:6]
        return {
            "part": part_number,
            "title": f"Part {part_number}",
            "thesis": lines[0] if lines else f"{title} part {part_number}",
            "key_points": lines[1:5],
            "evidence": [],
            "terms": [],
            "open_questions": [],
            "handoff": pending_concepts or "",
        }

    @staticmethod
    def format_digest_for_prompt(digest) -> str:
        if isinstance(digest, str):
            return digest
        if not isinstance(digest, dict):
            return str(digest or "(empty digest)")

        def bullets(values) -> str:
            if not values:
                return "- (none)"
            if isinstance(values, str):
                values = [values]
            rendered = [f"- {digest_value_to_text(v)}" for v in values if digest_value_to_text(v)]
            return "\n".join(rendered) or "- (none)"

        part = digest.get("part", "?")
        title = digest.get("title", f"Part {part}")
        return (
            f"### Part {part}: {title}\n"
            f"Thesis: {digest_value_to_text(digest.get('thesis', ''))}\n\n"
            "Key points:\n"
            f"{bullets(digest.get('key_points', []))}\n\n"
            "Evidence and source-grounded details:\n"
            f"{bullets(digest.get('evidence', []))}\n\n"
            "Terms:\n"
            f"{bullets(digest.get('terms', []))}\n\n"
            "Open questions:\n"
            f"{bullets(digest.get('open_questions', []))}\n\n"
            "Handoff:\n"
            f"{digest_value_to_text(digest.get('handoff', '')) or '(none)'}\n"
        )

    def generate_synthesis(
        self,
        title: str,
        part_digests: list,
        final_concepts: str,
        template: str | None = None,
        persona: str | None = None,
        *,
        critique_feedback: str | None = None,
    ) -> str:
        """Synthesize a long document from per-part digests.

        Synthesis is a fixed methodology, not a persona — so we hard-wire
        `persona='none'` and `operation='synthesize'` here by default, but allow
        overriding `persona` when needed. `template` controls only the output format.

        `critique_feedback` carries the findings of a failed critique postcheck
        on a previous attempt; when None the prompt is byte-identical to the
        no-retry path.
        """
        lang_hint = self._get_lang_hint()
        digest_text = "\n\n".join(self.format_digest_for_prompt(d) for d in part_digests)
        feedback_block = (
            f"Previous attempt was critiqued. Address these findings:\n{critique_feedback}\n\n"
            if critique_feedback
            else ""
        )
        prompt = (
            f'You have processed a long document titled "{title}" using a map-reduce pipeline.\n\n'
            f"Structured digests from each part:\n{digest_text}\n\n"
            f"Final unresolved concepts or carry-over notes:\n{final_concepts or '(none)'}\n\n"
            f"{feedback_block}"
            f"Task:\nWrite the final synthesis in {lang_hint}.\n"
        )
        resolved_persona = persona if persona is not None else "none"
        system_prompt, cap_resolution = self._build_system_prompt(
            "Create a source-grounded synthesis from structured part digests.",
            forced_template=template or settings.USE_TEMPLATE or "wiki-note",
            require_yaml_header=False,
            persona=resolved_persona,
            operation="synthesize",
        )

        try:
            return self._strip_accidental_frontmatter(
                self._complete_text(
                    system_prompt,
                    prompt,
                    temperature=0.25,
                    max_tokens=settings.MAX_OUTPUT,
                    trace_context={
                        "stage": "generate_synthesis",
                        "persona": resolved_persona,
                        "operation": "synthesize",
                        "template": template or settings.USE_TEMPLATE or "wiki-note",
                        "metadata": {
                            "title": title,
                            "part_count": len(part_digests),
                            "capability_resolution": cap_resolution,
                        },
                    },
                )
            )
        except Exception as e:
            logging.error(f"Synthesis failed for {title}: {e}")
            return f"Synthesis failed: {e}"

    def critique_text(
        self,
        candidate: str,
        sources: str,
        focus: str | None = None,
    ) -> str:
        """Evaluate a candidate text against its supporting sources.

        Critique is a fixed methodology — persona='none', operation='critique'.
        Output is freeform findings (severity-tagged bullets + verdict), so
        no Markdown template is forced.
        """
        lang_hint = self._get_lang_hint()
        prompt = (
            f"## Sources\n{sources}\n\n"
            f"## Candidate\n{candidate}\n\n"
            f"## Focus\n{focus or '(general)'}\n\n"
            f"Task:\nProduce the critique in {lang_hint} following the operating rules.\n"
        )
        system_prompt, cap_resolution = self._build_system_prompt(
            "Evaluate the candidate text against the provided sources and surface defects.",
            forced_template="none",
            require_yaml_header=False,
            persona="none",
            operation="critique",
        )
        try:
            return self._strip_accidental_frontmatter(
                self._complete_text(
                    system_prompt,
                    prompt,
                    temperature=0.1,
                    max_tokens=settings.MAX_OUTPUT,
                    trace_context={
                        "stage": "critique_text",
                        "persona": "none",
                        "operation": "critique",
                        "template": "none",
                        "metadata": {
                            "focus": focus,
                            "capability_resolution": cap_resolution,
                        },
                    },
                )
            )
        except Exception as e:
            logging.error(f"Critique failed: {e}")
            return f"Critique failed: {e}"

    def refute_insight(self, candidate: str, sources: list[str]) -> dict:
        """Run the refute operation to challenge an insight candidate."""
        try:
            system_prompt, cap_resolution = self._build_system_prompt(
                "Evaluate the candidate insight against the provided sources and try to refute it.",
                operation="refute",
                persona="none",
                forced_template="none",
                require_yaml_header=False,
            )
            sources_text = "\n\n".join(f"[{i+1}] {s}" for i, s in enumerate(sources))
            user_msg = (
                f"## Candidate Insight\n{candidate}\n\n"
                f"## Source Materials\n{sources_text}\n"
            )
            
            raw = self._complete_text(
                system_prompt,
                user_msg,
                temperature=0.1,
                trace_context={
                    "stage": "refute_insight",
                    "operation": "refute",
                    "metadata": {
                        "capability_resolution": cap_resolution,
                    },
                },
            )
            
            response_text = clean_llm_response(raw)
            verdict = None
            verdict_match = re.search(r'(?im)^\**\s*Verdict\**\s*[:：]\s*[*_`]*\s*(survived|refuted)', response_text)
            if verdict_match:
                verdict = verdict_match.group(1).lower()
            
            return {
                "verdict": verdict,
                "notes": response_text[:500]
            }
            
        except Exception as e:
            logging.error(f"LLM Error in refute_insight: {e}")
            return {
                "verdict": None,
                "notes": str(e)[:500]
            }

    # ─── Quality scoring (P0) ───────────────────────────────────────────

    def score_text_quality(self, text: str, prompt_version: str = "v1") -> dict:
        """Score a text chunk's self-containedness on a 1-10 scale.

        Uses a hardcoded temperature=0.0 and a version-locked prompt so
        regression runs are reproducible. Returns a dict:

            {"score": int 1-10, "reason": str, "prompt_version": str}

        On any failure (bad JSON, LLM error, missing version) returns
        {"score": 0, "reason": "<failure detail>", "prompt_version": ...}
        so callers can filter or median around failures rather than crash.
        """
        if not isinstance(text, str) or not text.strip():
            return {"score": 0, "reason": "empty text", "prompt_version": prompt_version}

        system_prompt = _CHUNK_COHERENCE_PROMPTS.get(prompt_version)
        if system_prompt is None:
            return {
                "score": 0,
                "reason": f"unknown prompt_version: {prompt_version!r}",
                "prompt_version": prompt_version,
            }

        user_msg = f'Chunk:\n"""\n{text}\n"""'
        # Version-locked P0 scorer: prompt is frozen. Bespoke (not _complete_json)
        # because the `reason` field must distinguish a transport failure from a
        # parse miss — both test-pinned. One reasoning-channel re-roll on a parse
        # miss before falling back to score 0.
        parsed = {}
        for attempt in range(2):
            try:
                raw = self._complete_text(
                    system_prompt=system_prompt,
                    user_msg=user_msg,
                    temperature=0.0,
                    max_tokens=None,
                    trace_context={
                        "stage": "score_text_quality",
                        "metadata": {"prompt_version": prompt_version, "json_attempt": attempt + 1},
                    },
                )
            except Exception as e:
                logging.warning(f"score_text_quality LLM call failed: {e}")
                return {"score": 0, "reason": f"llm error: {e}", "prompt_version": prompt_version}
            parsed = extract_json_object(raw)
            if parsed:
                break
            logging.warning(f"score_text_quality parse miss (attempt {attempt + 1}/2)")

        score = parsed.get("score")
        reason = parsed.get("reason", "")

        if not isinstance(score, (int, float)):
            return {
                "score": 0,
                "reason": "non-numeric score in response",
                "prompt_version": prompt_version,
            }
        # Clamp to [1, 10] — some models drift below or above the scale.
        score = max(1, min(10, int(round(score))))
        return {"score": score, "reason": str(reason)[:240], "prompt_version": prompt_version}

    # ─── Topic-shift detection (ThoughtfulSplitter Phase 4) ─────────────

    def find_topic_shifts(
        self,
        paragraphs: list[str],
        prompt_version: str = "v1",
    ) -> dict:
        """Ask the LLM which paragraph boundaries are clear topic shifts.

        Returns a dict with keys:
          - `split_after`: validated list of paragraph indices (1-based, each
            in [1, len(paragraphs) - 1]). At most 2 entries.
          - `prompt_version`: which prompt was used.

        On any failure (missing version, LLM error, bad JSON, no usable
        entries), `split_after` is `[]` — the caller treats this as
        "no topic shifts detected" and degrades gracefully.

        This method never raises.
        """
        if not isinstance(paragraphs, list) or len(paragraphs) < 3:
            # Less than 3 paragraphs → at most 1 possible split point, but a
            # 2-paragraph chunk rarely benefits from a topic-shift detector.
            # Return empty rather than burn an LLM call.
            return {"split_after": [], "prompt_version": prompt_version}

        system_prompt = _TOPIC_SHIFT_PROMPTS.get(prompt_version)
        if system_prompt is None:
            return {"split_after": [], "prompt_version": prompt_version}

        # Build the numbered-paragraph view. Strip trailing newlines so the
        # LLM doesn't see ragged spacing.
        user_msg_parts = [f"[P{i + 1}]\n{p.strip()}" for i, p in enumerate(paragraphs)]
        user_msg = (
            "Number of paragraphs: " + str(len(paragraphs)) + "\n\n"
            + "\n\n".join(user_msg_parts)
        )

        parsed = self._complete_json(
            kind="object",
            system_prompt=system_prompt,
            user_msg=user_msg,
            temperature=0.0,
            max_tokens=None,
            trace_context={
                "stage": "find_topic_shifts",
                "metadata": {
                    "prompt_version": prompt_version,
                    "paragraph_count": len(paragraphs),
                },
            },
        )
        raw_indices = parsed.get("split_after", [])
        validated = self._validate_topic_shifts(raw_indices, len(paragraphs))
        return {"split_after": validated, "prompt_version": prompt_version}

    # ─── Context summary (ThoughtfulSplitter Phase 5 / P6) ────────────

    def summarize_for_context(
        self,
        text: str,
        prompt_version: str = "v1",
        max_chars: int = 200,
    ) -> dict:
        """Generate a 1-2 sentence factual summary for use as context preamble.

        Returns:
            `{"summary": str, "prompt_version": str}`
            On any failure the summary is `""` — the caller should treat
            that as "no preamble available" and skip embedding.

        Never raises. Length is hard-capped at `max_chars` characters
        regardless of what the LLM returns.
        """
        if not isinstance(text, str) or not text.strip():
            return {"summary": "", "prompt_version": prompt_version}

        system_prompt = _SUMMARY_PROMPTS.get(prompt_version)
        if system_prompt is None:
            return {"summary": "", "prompt_version": prompt_version}

        parsed = self._complete_json(
            kind="object",
            system_prompt=system_prompt,
            user_msg=text,
            temperature=0.0,
            max_tokens=None,
            trace_context={
                "stage": "summarize_for_context",
                "metadata": {"prompt_version": prompt_version, "max_chars": max_chars},
            },
        )
        summary = parsed.get("summary", "")
        if not isinstance(summary, str):
            return {"summary": "", "prompt_version": prompt_version}

        # Collapse internal whitespace; cap length.
        summary = " ".join(summary.split())
        if len(summary) > max_chars:
            summary = summary[: max_chars - 1].rstrip() + "…"
        return {"summary": summary, "prompt_version": prompt_version}

    @staticmethod
    def _validate_topic_shifts(raw_indices, n_paragraphs: int) -> list[int]:
        """Defensive filter: in-range, integer-only, deduped, at most 2 entries.

        Accepts the LLM's list as-is; rejects entries that are clearly wrong
        (non-int, out of [1, N-1], duplicates). The first 2 surviving entries
        are returned in ascending order.
        """
        if not isinstance(raw_indices, list):
            return []
        valid: list[int] = []
        for x in raw_indices:
            if isinstance(x, bool):  # bool is a subclass of int — exclude.
                continue
            if not isinstance(x, (int, float)):
                continue
            idx = int(x)
            if idx < 1 or idx >= n_paragraphs:
                continue
            if idx in valid:
                continue
            valid.append(idx)
        valid.sort()
        return valid[:2]

    # ─── Dynamic Routing (Option C) ───────────────────────────────────

    def classify_document(self, filename: str, content_prefix: str) -> str:
        """Analyze the filename and content prefix to classify it into a category.

        Returns:
            A lowercase word like 'patent', 'paper', 'novel', etc.
        """
        system_prompt = (
            "You are an expert document classifier.\n"
            "Analyze the filename and the first 500 characters of the document content to classify it into a category.\n"
            "Examples of common categories: patent, paper, novel, finance, medical, legal, tutorial.\n"
            "If it matches a common category, output that category. Otherwise, determine a single-word lowercase category (slug) that best fits the document (e.g., 'recipe', 'diary').\n\n"
            "Return ONLY the category name as a single lowercase word (no punctuation, no markdown, no spaces)."
        )
        user_msg = f"Filename: {filename}\nContent prefix:\n{content_prefix}"
        try:
            raw = self._complete_text(
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=0.0,
                max_tokens=None,
                trace_context={
                    "stage": "classify_document",
                    "metadata": {"filename": filename},
                },
            )
            # Clean response to ensure only lowercase alphanumeric characters (and hyphen) are returned.
            category = raw.strip().lower()
            category = re.sub(r'[^a-z0-9\-]', '', category)
            return category or "default"
        except Exception as e:
            logging.warning(f"classify_document LLM call failed: {e}")
            return "default"

    def select_profile(self, filename: str, content_prefix: str, options: list[dict]) -> str:
        """Pick the best routing profile for a document from known options.

        Unlike classify_document (open-ended slug), this is a closed-choice
        selection over the registered profiles, so the answer is always
        actionable. `options` is a list of {"name", "hint"} dicts.

        Returns:
            A profile name from `options`, or "none" when nothing fits.
        """
        if not options:
            return "none"
        valid_names = {opt["name"] for opt in options}
        menu = "\n".join(f"- {opt['name']}: {opt['hint']}" for opt in options)
        system_prompt = (
            "You are a document router. Choose which profile should handle the document.\n"
            "Available profiles (name: when to use):\n"
            f"{menu}\n\n"
            "Pick the single best-fitting profile. If none of them genuinely fits, answer exactly 'none'.\n"
            "Return ONLY the profile name (or 'none') as a single lowercase token — no punctuation, no explanation."
        )
        user_msg = f"Filename: {filename}\nContent prefix:\n{content_prefix}"
        try:
            raw = self._complete_text(
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=0.0,
                max_tokens=None,
                trace_context={
                    "stage": "select_profile",
                    "metadata": {"filename": filename, "options": sorted(valid_names)},
                },
            )
            choice = re.sub(r'[^a-z0-9\-]', '', raw.strip().lower())
            if choice in valid_names:
                return choice
            # Salvage: the model wrapped the name in prose ("I choose academic.").
            # Only safe when exactly one registered name survives in the answer.
            if choice and choice != "none":
                contained = [name for name in sorted(valid_names) if name in choice]
                if len(contained) == 1:
                    logging.info(
                        f"select_profile: salvaged {contained[0]!r} from model answer {choice!r}."
                    )
                    return contained[0]
                logging.warning(
                    f"select_profile: model answered {choice!r}, not a registered profile; treating as none."
                )
            return "none"
        except Exception as e:
            logging.warning(f"select_profile LLM call failed: {e}")
            return "none"

    def extract_claims(self, insight_text: str) -> list[dict]:
        """Distill an insight report into at most 3 atomic claims.

        Each claim must be an independently truth-evaluable statement
        (not a topic label). Returns [{"claim": ..., "summary": ..., "applies_when": ...}];
        empty list on any failure (fail-open — the insight just waits).
        """
        system_prompt = (
            "You distill an insight report into atomic claims for a long-term memory store.\n"
            "Extract AT MOST 3 claims. Each claim must be:\n"
            "- ONE declarative sentence that can be judged true or false on its own\n"
            "  (NOT a topic label like 'memory and learning').\n"
            "- In the same language as the report.\n"
            "- Self-contained: no dangling pronouns or 'this/it' references.\n"
            "- 'Atomic' does not mean unconditional. Condition-based claims (e.g. 'Under X, A causes B') are better than vague absolutes.\n\n"
            "Return ONLY a JSON array:\n"
            '[{"claim": "<one sentence>", "summary": "<one-line gist>", "applies_when": "<specific context/condition this applies to>"}]\n'
            "No prose outside the JSON. Return [] if the report contains no real claim."
        )
        try:
            parsed = self._complete_json(
                kind="array",
                system_prompt=system_prompt,
                user_msg=insight_text,
                temperature=0.2,
                max_tokens=1024,
                trace_context={"stage": "extract_claims", "metadata": {}},
            )
            out = []
            for item in parsed if isinstance(parsed, list) else []:
                if not isinstance(item, dict):
                    continue
                claim = item.get("claim")
                if isinstance(claim, str) and len(claim.strip()) >= 8:
                    out.append({
                        "claim": claim.strip(),
                        "summary": str(item.get("summary") or "").strip()[:200],
                        "applies_when": str(item.get("applies_when") or "").strip(),
                    })
            return out[:3]
        except Exception as e:
            logging.warning(f"extract_claims failed: {e}")
            return []

    def _assess_falsifiability_once(self, claim: str) -> dict:
        system_prompt = (
            "You are assessing the falsifiability (empirical content) of a claim.\n"
            "A claim has empirical content if and only if you can describe a concrete observation that would prove it false.\n\n"
            "First, try to write a 'falsifier' — a concrete, observable scenario that would refute the claim.\n"
            "Then, score the claim from 0.0 to 1.0 based on how falsifiable it is:\n"
            "- 1.0: The falsifier is a concrete, observable, specific scenario.\n"
            "- 0.5: A falsifier exists but requires further operationalization to be tested.\n"
            "- 0.0: The claim is unfalsifiable (e.g., vague absolute, tautology, value statement, or the falsifier is just 'when it is not true').\n\n"
            "Return ONLY a JSON object:\n"
            '{"score": <float 0.0, 0.5, or 1.0>, "falsifier": "<specific observation that refutes it, <=200 chars>", '
            '"falsifier_zh": "<the same falsifier translated into Traditional Chinese (繁體中文), <=200 chars>"}'
        )
        fallback = {"score": None, "falsifier": ""}
        # Transport retries live in _complete_text; this loop covers a
        # different failure — reasoning models intermittently return empty
        # or unparseable text without raising. One re-roll usually lands.
        for attempt in range(2):
            try:
                raw = self._complete_text(
                    system_prompt=system_prompt,
                    user_msg=f"Claim: {claim}",
                    temperature=0.1,
                    max_tokens=None,  # reasoning models need thinking room
                    trace_context={"stage": "assess_falsifiability", "metadata": {"attempt": attempt + 1}},
                )
                parsed = extract_json_object(raw)
                if isinstance(parsed, dict):
                    score = parsed.get("score")
                    if isinstance(score, (int, float)) and not isinstance(score, bool):
                        falsifier = str(parsed.get("falsifier") or "").strip()[:200]
                        # 英文為主、中文輔助：append the zh gloss when present
                        # so the report stays readable for the user.
                        zh = str(parsed.get("falsifier_zh") or "").strip()[:200]
                        if zh and zh != falsifier:
                            falsifier = f"{falsifier}（{zh}）"
                        # Clamp: an out-of-range score must not leak into the
                        # confidence formula (0.3 + 0.4*s) and blow past 1.0.
                        return {
                            "score": max(0.0, min(1.0, float(score))),
                            "falsifier": falsifier,
                        }
                logging.warning(f"assess_falsifiability: unparseable output (attempt {attempt + 1})")
            except Exception as e:
                logging.warning(f"assess_falsifiability failed (attempt {attempt + 1}): {e}")
        return fallback

    def assess_falsifiability(self, claim: str) -> dict:
        """Assess whether a claim is falsifiable (has empirical content).

        每個新主張 ≈ samples × call（本地模型）。
        Returns {"score": float 0-1, "falsifier": "<max 200 chars>"}.
        Fail-open returns {"score": None, "falsifier": ""}.
        """
        import statistics
        from core.config import CORTEX_FALSIFY_SAMPLES

        if CORTEX_FALSIFY_SAMPLES == 1:
            return self._assess_falsifiability_once(claim)

        results = []
        for _ in range(CORTEX_FALSIFY_SAMPLES):
            res = self._assess_falsifiability_once(claim)
            if res["score"] is not None:
                results.append(res)

        if not results:
            return {"score": None, "falsifier": ""}

        scores = [r["score"] for r in results]
        median_score = statistics.median(scores)
        median_score = max(0.0, min(1.0, round(median_score, 4)))

        best_result = min(results, key=lambda r: abs(r["score"] - median_score))
        return {
            "score": median_score,
            "falsifier": best_result["falsifier"]
        }

    def adjudicate_claims(self, claim_a: str, claim_b: str) -> dict:
        """Closed-choice relation verdict between two atomic claims.

        equivalent is BIDIRECTIONAL entailment only — that criterion is
        the merge trigger (CortexMemory invariant 5), so it is spelled
        out in the prompt. Any parse failure or illegal verdict degrades
        to "unrelated": the conservative outcome (no merge, no link).
        """
        system_prompt = (
            "You judge the logical relation between two atomic claims, A and B.\n"
            "Choose EXACTLY ONE verdict:\n"
            "- equivalent:    A and B entail each other IN BOTH DIRECTIONS — interchangeable\n"
            "                 statements of the same claim. If A is merely a special case of B\n"
            "                 (or vice versa), that is NOT equivalent.\n"
            "- entails:       A is the more specific claim; A being true makes B true, not vice versa.\n"
            "- entailed_by:   B is the more specific claim; B being true makes A true, not vice versa.\n"
            "- complementary: same topic, different non-conflicting aspects.\n"
            "- contradicts:   they cannot both be true.\n"
            "- unrelated:     different topics.\n\n"
            "Return ONLY a JSON object:\n"
            '{"verdict": "<one of the six>", "rationale": "<=200 chars>"}'
        )
        valid = {"equivalent", "entails", "entailed_by", "complementary", "contradicts", "unrelated"}
        fallback = {"verdict": "unrelated", "rationale": "adjudication failed; conservative default"}
        try:
            parsed = self._complete_json(
                kind="object",
                system_prompt=system_prompt,
                user_msg=f"A: {claim_a}\n\nB: {claim_b}",
                temperature=0.0,
                max_tokens=None,  # reasoning models need thinking room
                trace_context={"stage": "adjudicate_claims", "metadata": {}},
            )
            verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
            if isinstance(verdict, str) and verdict.strip().lower() in valid:
                return {
                    "verdict": verdict.strip().lower(),
                    "rationale": str(parsed.get("rationale") or "").strip()[:200],
                }
            logging.warning(f"adjudicate_claims: illegal verdict {verdict!r}; defaulting to unrelated")
            return fallback
        except Exception as e:
            logging.warning(f"adjudicate_claims failed: {e}")
            return fallback

    def generate_bench_question(self, title: str, thesis: str) -> str:
        """Turn a page's thesis into one natural retrieval question.

        Used by the bench builder to auto-grow the regression suite: the
        question must be answerable by the page but NOT quote the thesis
        verbatim (a verbatim copy would test nothing — its embedding is
        already near-identical to the facet's).
        """
        system_prompt = (
            "You write retrieval test queries for a personal knowledge base.\n"
            "Given a document title and its thesis, write ONE natural question a user "
            "might ask that this document should answer.\n"
            "Rules:\n"
            "- Same language as the thesis.\n"
            "- Do NOT copy the thesis verbatim; paraphrase into a question.\n"
            "- Do NOT mention the document title.\n"
            "- Return ONLY the question, one line, no quotes or commentary."
        )
        user_msg = f"Title: {title}\nThesis: {thesis}"
        try:
            raw = self._complete_text(
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=0.3,
                max_tokens=400,
                trace_context={
                    "stage": "generate_bench_question",
                    "metadata": {"title": title},
                },
            )
            question = raw.strip().splitlines()[0].strip().strip('"').strip()
            return question if len(question) >= 8 else ""
        except Exception as e:
            logging.warning(f"generate_bench_question failed for {title}: {e}")
            return ""

    def generate_persona_and_template(self, category: str) -> dict:
        """Dynamically generate a Persona note and a Markdown Template for a new document category.

        Returns:
            A dict with 'persona_name', 'persona_content', 'template_name', and 'template_content'.
        """
        system_prompt = (
            "We need to dynamically generate a Persona note and a Markdown Template for a new document category: \"{category}\".\n\n"
            "1. Persona: A markdown document defining the role, traits, and guidelines for an AI agent handling this type of document.\n"
            "2. Template: A markdown template detailing the structure, sections, and layout of the synthesized output for this type of document.\n\n"
            "Generate BOTH. Output MUST be valid JSON with the following structure:\n"
            "{{\n"
            "    \"persona_name\": \"Suggested filename for persona, e.g., 'novel-assistant' (use lowercase-hyphenated slug)\",\n"
            "    \"persona_content\": \"Markdown content for the persona file\",\n"
            "    \"template_name\": \"Suggested filename for template, e.g., 'novel-summary' (use lowercase-hyphenated slug)\",\n"
            "    \"template_content\": \"Markdown content for the template file\"\n"
            "}}\n\n"
            "Ensure the response is raw JSON."
        ).format(category=category)

        user_msg = f"Generate the persona and template for category: {category}"
        try:
            parsed = self._complete_json(
                kind="object",
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=0.3,
                max_tokens=2048,
                trace_context={
                    "stage": "generate_persona_and_template",
                    "metadata": {"category": category},
                },
            )
            return parsed or {}
        except Exception as e:
            logging.warning(f"generate_persona_and_template LLM call failed: {e}")
            return {}

