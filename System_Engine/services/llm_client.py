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
import re
import time
from pathlib import Path
from typing import Any


from core.config import (
    LLM_PROVIDER,
    OPERATIONS_DIR,
    SKILLS_DIR,
    settings,
)
from core.parser import (
    extract_json_array,
    extract_json_object,
    is_empty_json_literal,
    clean_llm_response,
)
from core.json_extract import salvage_json_array
from core.retrying import reroll, retry_call
from core.utils import MtimeCache, digest_value_to_text
from services.capability_manager import CapabilityManager
from services.trace_store import TraceStore, elapsed_ms


# ─── Constants ────────────────────────────────────────────────────────

# User-message field labels, keyed by the localized-content suffix (".zh"/".ja")
# from `_localized_suffix()`. Keying on the suffix — not on `_get_lang_hint()`'s
# free text — avoids a silent fallback to English labels (the hint string is
# longer than any map key, so the old `_get_lang_hint()` lookup never matched).
_LABELS_BY_SUFFIX = {
    ".zh": {"file": "檔案名稱", "content": "素材內容"},
    ".ja": {"file": "ファイル名", "content": "素材内容"},
}
_DEFAULT_LABELS = {"file": "Filename", "content": "Content"}

# Cortex claim-building prompts. The live source is Templates/Prompts/
# cortex_extract_claims.md / cortex_falsifiability.md (vault-editable + M3-
# reachable); these constants are the fallback used if the vault file is missing
# so the nightly consolidation never breaks. Keep the two in sync.
_CORTEX_EXTRACT_CLAIMS_PROMPT = (
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
_CORTEX_FALSIFIABILITY_PROMPT = (
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

# Version-locked quality/splitter prompts now live in services/llm/task_prompts.py
# (P2b). Old underscore names kept as aliases for in-module callers.
from services.llm.task_prompts import (  # noqa: E402
    CHUNK_COHERENCE_PROMPTS as _CHUNK_COHERENCE_PROMPTS,
    SUMMARY_PROMPTS as _SUMMARY_PROMPTS,
    TOPIC_SHIFT_PROMPTS as _TOPIC_SHIFT_PROMPTS,
)

# Provider machinery now lives in services/llm/transport.py (P2b). The old
# underscore names stay importable from here — tests and callers use them.
from services.llm import prompt_composer, response_parsing  # noqa: E402
from services.llm import transport as _transport  # noqa: E402
from services.llm.prompt_composer import PromptComposer  # noqa: E402
from services.llm.transport import (  # noqa: E402
    _genai,
    is_non_retryable_llm_error as _is_non_retryable_llm_error,  # noqa: F401  (test import surface)
    is_transient_llm_error as _is_transient_llm_error,
)


# ─── Client ────────────────────────────────────────────────────────────


class LLMClient:
    def __init__(self):
        self.provider = LLM_PROVIDER
        self._file_cache = MtimeCache()
        self.trace_store = TraceStore()
        self.capability_manager = CapabilityManager(OPERATIONS_DIR, SKILLS_DIR)

        self.composer = PromptComposer(self._file_cache, self.capability_manager)
        self.client, self.model = _transport.build_client(self.provider)

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
        # Thin delegate: tests monkeypatch this method; transport owns the logic.
        return _transport.complete_once(
            self.provider, self.client, self.model, system_prompt, user_msg, temperature, max_tokens
        )

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
        def _record_attempt(attempt: int):
            retry_meta["retry_attempts"] = attempt

        def _record_error(attempt: int, e: Exception):
            retry_meta["retry_last_error"] = str(e)
            if _is_transient_llm_error(e):
                retry_meta["retry_transient"] = True

        return retry_call(
            lambda: self._complete_provider_text_once(
                system_prompt, user_msg, temperature, max_tokens
            ),
            retries=retries,
            initial_delay=initial_delay,
            backoff_factor=backoff_factor,
            is_retryable=_is_transient_llm_error,
            on_attempt=_record_attempt,
            on_error=_record_error,
            log_label="LLM provider call",
        )

    def trace_run(self, **kwargs):
        return self.trace_store.run(**kwargs)

    def current_trace_ids(self) -> list[str]:
        return self.trace_store.current_trace_ids()

    def current_run_id(self) -> str | None:
        return self.trace_store.current_run_id()

    # ─── Localized prompt loading ───────────────────────────────────────

    # Thin delegates — composition logic lives in services/llm/prompt_composer.py
    # (P2b). Kept as methods because tests and in-module callers use these names.

    def _get_lang_hint(self) -> str:
        return prompt_composer.lang_hint()

    def _localized_suffix(self) -> str:
        return prompt_composer.localized_suffix()

    def _load_localized_content(self, file_path: Path) -> str:
        return self.composer.load_localized_content(file_path)

    def _load_capability_body(self, file_path: Path) -> str:
        return self.composer.load_capability_body(file_path)

    def _load_project_identity(self) -> str:
        return self.composer.load_project_identity()

    def _vault_prompt(self, filename: str, fallback: str) -> str:
        """A system prompt sourced from `Templates/Prompts/<filename>` so it is
        vault-editable and hot-reloaded (mtime cache) like every other prompt —
        and reachable by the self-improvement arc (M3). Falls back to the
        built-in text if the file is absent/unreadable, so a missing vault file
        never breaks the nightly pipeline."""
        from core.config import PROMPTS_DIR

        path = PROMPTS_DIR / filename
        try:
            cache = getattr(self, "_file_cache", None)
            if cache is not None:
                text = cache.read(path)
            else:
                text = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            text = ""
        return text.strip() if text.strip() else fallback

    def _build_system_prompt(
        self,
        instruction_type: str,
        forced_template: str | None = None,
        default_template: str | None = None,
        require_yaml_header: bool = True,
        persona: str | None = None,
        operation: str | None = None,
    ) -> tuple[str, dict]:
        return self.composer.build_system_prompt(
            instruction_type,
            forced_template=forced_template,
            default_template=default_template,
            require_yaml_header=require_yaml_header,
            persona=persona,
            operation=operation,
        )

    # ─── Output cleanup ─────────────────────────────────────────────────
    # Logic lives in services/llm/response_parsing.py (P2b); kept as
    # staticmethods for existing callers/tests.
    _strip_accidental_frontmatter = staticmethod(response_parsing.strip_accidental_frontmatter)
    _hybrid_parse = staticmethod(response_parsing.hybrid_parse)

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
        labels = _LABELS_BY_SUFFIX.get(self._localized_suffix(), _DEFAULT_LABELS)

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
                # End-of-message language pin: the part path has no other
                # user-turn reminder (unlike synthesis), and the directive in
                # the system prompt can be outweighed by an English template.
                user_text += f"\n\n（請以 {self._get_lang_hint()} 輸出整篇,包含所有章節標題。）"
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

    def _build_multimodal_user_msg(
        self, image_path: Path, filename: str | None, labels: dict
    ) -> Any:
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
            user_msg = f"## User Directive\n{query_content}\n\n## Provided Source Text\n{ctx}\n"
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
        pin_language: bool = False,
    ) -> str:
        """Lean completion with a caller-supplied system prompt.

        Unlike answer_query, this injects NO persona / template / visualization
        scaffolding — the system prompt is used verbatim. For controlled tasks
        (e.g. Cortex recall's select-and-summarize) that must not inherit the
        Q&A document machinery (which made the model chase a Mermaid diagram and
        dump its scratchpad). Returns "" on failure (fail-open).

        ``pin_language=True`` prepends the OUTPUT-LANGUAGE banner (P4): use it for
        USER-VISIBLE prose that must honor OUTPUT_LANGUAGE but has no language
        guarantee of its own. Do NOT set it for strict-JSON extraction (the
        banner pollutes the schema prompt) or for output whose language should
        follow the content rather than OUTPUT_LANGUAGE (e.g. learning-aid
        artifacts, which carry their own content-language rule)."""
        if pin_language:
            system_prompt = f"{prompt_composer.language_banner()}\n\n{system_prompt}"
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

    def _complete_json(
        self, *, kind: str = "object", raise_on_miss: bool = False, **complete_kwargs
    ):
        """Complete a JSON-output prompt with one reasoning-channel re-roll.

        Transport-level retries already live in `_complete_text`. This guards a
        different, silent failure: reasoning models (gemma via Ollama) sometimes
        emit the whole reply into the reasoning channel and leave the content
        with no parseable JSON — no exception is raised. One re-roll usually
        lands it. (Same failure that silently zeroed LingLens extraction; see
        roadmap R4.)

        `kind` is "array" or "object". Returns the parsed list/dict, or the
        empty container after a second miss so callers keep their fail-open
        paths. With ``raise_on_miss=True``, a second miss raises instead so a
        caller can distinguish operational failure from a semantic empty.
        A genuinely empty answer — the model emitted a literal [] / {} —
        is returned WITHOUT a re-roll, so a real zero is never mistaken for a
        parse miss (mirrors the LingLens extraction rule). Per-attempt
        exceptions are caught and re-rolled.

        Callers must pass system_prompt/user_msg as keywords. Bespoke retry
        logic with stricter accept conditions (e.g. `_assess_falsifiability_once`,
        which re-rolls until the score itself parses) stays as-is.
        """
        parse = extract_json_array if kind == "array" else extract_json_object
        empty: list | dict = [] if kind == "array" else {}
        base_trace = dict(complete_kwargs.pop("trace_context", {}) or {})
        base_meta = dict(base_trace.get("metadata") or {})
        for attempt in range(2):
            trace = {**base_trace, "metadata": {**base_meta, "json_attempt": attempt + 1}}
            try:
                raw = self._complete_text(trace_context=trace, **complete_kwargs)
            except Exception as e:
                logging.warning(
                    f"_complete_json({kind}) call failed (attempt {attempt + 1}/2): {e}"
                )
                continue
            parsed = parse(raw)
            if parsed:
                return parsed
            if is_empty_json_literal(raw, kind):
                return empty  # whole reply IS [] / {} — genuine empty, not a parse miss
            logging.warning(f"_complete_json({kind}) parse miss (attempt {attempt + 1}/2)")
        if raise_on_miss:
            raise ValueError(f"No parseable JSON {kind} after 2 attempts")
        return empty

    _LANG_NAMES = {
        "en": "English",
        "zh": "Traditional Chinese",
        "de": "German",
        "ja": "Japanese",
        "fr": "French",
        "es": "Spanish",
    }

    def translate_query(self, text: str, target_langs: list[str]) -> dict:
        """Translate a search query into each target language, as
        ``{lang_code: translation}`` — used to widen the cross-lingual
        retrieval candidate net (see services/cross_lingual.py).

        Routes through `_complete_json` (transport retry + centralized trace),
        and caches per (text, langs) so repeated queries cost nothing. Returns
        only the langs the model actually produced; {} on miss (fail-open).
        """
        if not text or not text.strip() or not target_langs:
            return {}
        cache = self.__dict__.setdefault("_translate_query_cache", {})
        key = (text.strip(), tuple(target_langs))
        if key in cache:
            return cache[key]
        named = ", ".join(f"{c} ({self._LANG_NAMES.get(c, c)})" for c in target_langs)
        system_prompt = (
            "You translate SEARCH QUERIES for cross-lingual document retrieval. "
            "Preserve technical terms, proper nouns and named entities; keep it "
            "concise and faithful to search intent (do not answer the query). "
            f"Return ONLY a JSON object mapping each language code to its "
            f"translation. Target languages: {named}."
        )
        result = self._complete_json(
            kind="object",
            system_prompt=system_prompt,
            user_msg=f"Query: {text}",
            temperature=0.1,
            trace_context={
                "stage": "translate_query",
                "metadata": {"target_langs": list(target_langs)},
            },
        )
        # Keep only requested codes with non-empty string values.
        cleaned = {
            c: result[c].strip()
            for c in target_langs
            if isinstance(result.get(c), str) and result[c].strip()
        }
        cache[key] = cleaned
        return cleaned

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
        hl_max = max(0, int(settings.HIGHLIGHT_MAX))
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
            '  "handoff": "what the next or final synthesis must remember",\n'
            f'  "highlights": ["up to {hl_max} spans copied VERBATIM from the generated part note — the lines a reader most needs to notice"]\n'
            "}\n\n"
            "Rules:\n"
            "- Prefer specific details over generic summary language.\n"
            "- Do not invent facts that are not supported by the source chunk or generated note.\n"
            "- Keep each list item concise but information-rich.\n"
            "- highlights MUST be copied character-for-character from the GENERATED PART NOTE above "
            "(not the raw source chunk) and never paraphrased — a deterministic pass locates each span "
            "by exact match, so any drift silently drops the highlight.\n"
            f"- Choose at most {hl_max} highlights, the most essential only; fewer is better than padding. "
            "Each should be a complete sentence or clause, not a single word, and must not itself contain '=='.\n"
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
        parsed.setdefault("highlights", [])
        return parsed

    def _part_digest_fallback(
        self, title: str, part_number: int, part_note: str, pending_concepts: str
    ) -> dict:
        """Degraded digest used when the structured digest call fails.

        Only prose lines become thesis/key_points — headings, tables, and
        fences are template scaffolding, and feeding them downstream poisons
        both the facet index and the synthesis (observed live: cloud_act
        Part 1 shipped key_points of 「摘要」/「翻譯內文」). The ``degraded``
        flag lets the pipeline surface the failure and skip facet indexing;
        handoff stays empty rather than masquerading pending_concepts as one.
        """
        first_heading = ""
        prose: list[str] = []
        for raw in self._strip_accidental_frontmatter(part_note).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if not first_heading:
                    first_heading = line.lstrip("#").strip().strip("*").strip()
                continue
            if line.startswith(("|", "```", ">", "---", "![", "[[")):
                continue
            line = line.strip("-*• \t")
            # Too short to be a content sentence: emoji section labels,
            # separators, stray list markers.
            if len(line) < 12:
                continue
            prose.append(line)
            if len(prose) >= 5:
                break
        thesis = first_heading or (prose[0] if prose else f"{title} part {part_number}")
        return {
            "part": part_number,
            "title": f"Part {part_number}",
            "thesis": thesis,
            "key_points": [p for p in prose if p != thesis][:4],
            "evidence": [],
            "terms": [],
            "open_questions": [],
            "handoff": "",
            "highlights": [],
            "degraded": True,
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

    # Guardrail appended to the refute user message when the candidate comes
    # from a non-literal operation (analogy / fable / dialogue / counterfactual).
    # These operations DELIBERATELY reason non-literally: an analogy honestly
    # marks where it breaks ("tear lines"), a counterfactual reasons under a
    # negated premise, a dialogue stages disagreement, a fable is a story. The
    # strict prompt's "over-generalizations beyond the sources" criterion reads
    # those self-declared limits as refutation evidence and kills the insight —
    # punishing exactly the epistemic honesty the operation was designed to
    # produce (2026-07-12 audit: analogy 0710, groundedness 0.867, all facts
    # correct, still refuted). Under lenient mode the reviewer judges only the
    # transferable claim, not the non-literal vehicle carrying it.
    _REFUTE_LENIENT_GUARD = (
        "\n\n## Evaluation Mode: non-literal operation ({kind})\n"
        "This candidate is a **{kind}** — a deliberately non-literal thinking form, "
        "NOT a literal factual claim. Its self-declared limitations are a design "
        "feature, not a flaw. Do NOT treat any of the following as grounds to refute:\n"
        '- a passage that honestly marks where an analogy/mapping breaks ("tear lines");\n'
        "- reasoning that follows from a hypothetical or counterfactual premise;\n"
        "- staged disagreement between voices in a dialogue;\n"
        "- narrative or figurative framing in a fable.\n"
        "Refute ONLY if the core **transferable principle** (the moral / verdict / "
        "final recommendation the piece asks you to carry away) is factually "
        "contradicted by the sources, or misrepresents a mechanism the sources "
        "actually describe. If the transferable principle holds and only the "
        "vehicle is figurative, let it survive.\n"
    )

    def refute_insight(
        self,
        candidate: str,
        sources: list[str],
        *,
        lenient: bool = False,
        candidate_kind: str | None = None,
    ) -> dict:
        """Run the refute operation to challenge an insight candidate.

        `lenient=True` (set for non-literal operations via skill frontmatter
        `refute_mode: lenient`) appends a guardrail so the reviewer judges the
        transferable claim rather than the figurative vehicle. `candidate_kind`
        names the operation (e.g. "analogy") for the guardrail text.
        """
        try:
            system_prompt, cap_resolution = self._build_system_prompt(
                "Evaluate the candidate insight against the provided sources and try to refute it.",
                operation="refute",
                persona="none",
                forced_template="none",
                require_yaml_header=False,
            )
            sources_text = "\n\n".join(f"[{i + 1}] {s}" for i, s in enumerate(sources))
            user_msg = f"## Candidate Insight\n{candidate}\n\n## Source Materials\n{sources_text}\n"
            if lenient:
                user_msg += self._REFUTE_LENIENT_GUARD.format(kind=candidate_kind or "creative")

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
            verdict_match = re.search(
                r"(?im)^\**\s*Verdict\**\s*[:：]\s*[*_`]*\s*(survived|refuted)", response_text
            )
            if verdict_match:
                verdict = verdict_match.group(1).lower()

            return {"verdict": verdict, "notes": response_text[:500]}

        except Exception as e:
            logging.error(f"LLM Error in refute_insight: {e}")
            return {"verdict": None, "notes": str(e)[:500]}

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
            "Number of paragraphs: " + str(len(paragraphs)) + "\n\n" + "\n\n".join(user_msg_parts)
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
            category = re.sub(r"[^a-z0-9\-]", "", category)
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
            choice = re.sub(r"[^a-z0-9\-]", "", raw.strip().lower())
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

    def extract_claims_result(self, insight_text: str) -> dict:
        """Distill an insight report into at most 3 atomic claims.

        Each claim must be an independently truth-evaluable statement
        (not a topic label). ``valid`` distinguishes a successful empty
        extraction (the insight retracts every prior claim) from an LLM or
        parse failure (no verdict; the insight remains owed).
        """
        system_prompt = self._vault_prompt(
            "cortex_extract_claims.md", _CORTEX_EXTRACT_CLAIMS_PROMPT
        )
        try:
            parsed = self._complete_json(
                kind="array",
                raise_on_miss=True,
                system_prompt=system_prompt,
                user_msg=insight_text,
                temperature=0.2,
                max_tokens=1024,
                trace_context={"stage": "extract_claims", "metadata": {}},
            )
            if not isinstance(parsed, list):
                return {"valid": False, "claims": []}
            out = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                claim = item.get("claim")
                if isinstance(claim, str) and len(claim.strip()) >= 8:
                    out.append(
                        {
                            "claim": claim.strip(),
                            "summary": str(item.get("summary") or "").strip()[:200],
                            "applies_when": str(item.get("applies_when") or "").strip(),
                        }
                    )
            return {"valid": True, "claims": out[:3]}
        except Exception as e:
            logging.warning(f"extract_claims failed: {e}")
            return {"valid": False, "claims": []}

    def extract_claims(self, insight_text: str) -> list[dict]:
        """Compatibility wrapper for callers that only consume claims."""
        result = self.extract_claims_result(insight_text)
        claims = result.get("claims")
        return claims if isinstance(claims, list) else []

    def generate_structured(self, prompt: str, schema: dict) -> dict:
        import json

        system_prompt = "You are a helpful assistant. Output strictly valid JSON that matches the provided schema."
        user_msg = f"{prompt}\n\nSchema:\n{json.dumps(schema, ensure_ascii=False)}"
        try:
            parsed = self._complete_json(
                kind="object",
                system_prompt=system_prompt,
                user_msg=user_msg,
                temperature=0.2,
                max_tokens=1000,
                trace_context={"stage": "generate_structured"},
            )
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            logging.warning(f"generate_structured failed: {e}")
            return {}

    def _assess_falsifiability_once(self, claim: str) -> dict:
        system_prompt = self._vault_prompt(
            "cortex_falsifiability.md", _CORTEX_FALSIFIABILITY_PROMPT
        )

        # Transport retries live in _complete_text; this re-roll covers a
        # different failure — reasoning models intermittently return empty
        # or unparseable text without raising. One re-roll usually lands.
        def _attempt(attempt: int) -> dict | None:
            raw = self._complete_text(
                system_prompt=system_prompt,
                user_msg=f"Claim: {claim}",
                temperature=0.1,
                max_tokens=None,  # reasoning models need thinking room
                trace_context={
                    "stage": "assess_falsifiability",
                    "metadata": {"attempt": attempt},
                },
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
            logging.warning(f"assess_falsifiability: unparseable output (attempt {attempt})")
            return None

        return reroll(
            _attempt,
            lambda r: r is not None,
            attempts=2,
            fallback={"score": None, "falsifier": ""},
            swallow_errors=True,
            on_error=lambda a, e: logging.warning(
                f"assess_falsifiability failed (attempt {a}): {e}"
            ),
        )

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
        return {"score": median_score, "falsifier": best_result["falsifier"]}

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
        valid = {
            "equivalent",
            "entails",
            "entailed_by",
            "complementary",
            "contradicts",
            "unrelated",
        }
        fallback = {
            "verdict": "unrelated",
            "rationale": "adjudication failed; conservative default",
            # Callers must not persist this synthetic verdict as if the model
            # had actually judged the pair.
            "valid": False,
        }

        # Re-roll until a valid verdict parses. A reasoning model (gemma) sometimes
        # emits the whole reply into the reasoning channel, leaving unparseable
        # content — which silently degraded to "unrelated". Because "equivalent" is
        # the MERGE trigger, that lost real merges (2026-07-13 A3 diagnosis: it hit
        # a clear equivalent live). attempt 1 stays at temp 0 for a deterministic
        # verdict; retries warm up to escape the stuck empty-content sampling.
        def _attempt(attempt: int) -> dict | None:
            raw = self._complete_text(
                system_prompt=system_prompt,
                user_msg=f"A: {claim_a}\n\nB: {claim_b}",
                temperature=0.0 if attempt == 1 else 0.3,
                max_tokens=None,  # reasoning models need thinking room
                trace_context={"stage": "adjudicate_claims", "metadata": {"attempt": attempt}},
            )
            parsed = extract_json_object(raw)
            if isinstance(parsed, dict):
                verdict = parsed.get("verdict")
                if isinstance(verdict, str) and verdict.strip().lower() in valid:
                    return {
                        "verdict": verdict.strip().lower(),
                        "rationale": str(parsed.get("rationale") or "").strip()[:200],
                        "valid": True,
                    }
            logging.warning(f"adjudicate_claims: unparseable/illegal verdict (attempt {attempt})")
            return None

        return reroll(
            _attempt, lambda r: r is not None, attempts=3, fallback=fallback, swallow_errors=True
        )

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
            'We need to dynamically generate a Persona note and a Markdown Template for a new document category: "{category}".\n\n'
            "1. Persona: A markdown document defining the role, traits, and guidelines for an AI agent handling this type of document.\n"
            "2. Template: A markdown template detailing the structure, sections, and layout of the synthesized output for this type of document.\n\n"
            "Generate BOTH. Output MUST be valid JSON with the following structure:\n"
            "{{\n"
            '    "persona_name": "Suggested filename for persona, e.g., \'novel-assistant\' (use lowercase-hyphenated slug)",\n'
            '    "persona_content": "Markdown content for the persona file",\n'
            '    "template_name": "Suggested filename for template, e.g., \'novel-summary\' (use lowercase-hyphenated slug)",\n'
            '    "template_content": "Markdown content for the template file"\n'
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

    # Truncation/malformed-entry-tolerant array parse; logic lives in
    # core.json_extract (P1). Kept as a staticmethod for existing callers/tests.
    _parse_json_array = staticmethod(salvage_json_array)
