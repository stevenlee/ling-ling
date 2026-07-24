"""Provider transport for LLM completions (P2b of the refactor roadmap).

Everything provider-specific from services/llm_client.py lives here: client
construction (vllm / ollama / gemini), the single-shot completion call with
the reasoning-channel fallback, usage-count extraction, and transient-error
classification. Module functions (not a class) so LLMClient keeps its plain
`provider` / `client` / `model` attributes — tests set them directly.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any
from urllib.parse import urlparse

from core.config import settings
from services.trace_store import usage_to_counts

# ─── Lazy provider SDK imports ────────────────────────────────────────

_GENAI_MOD = None


def _genai():
    """Lazy-load google.genai (only when gemini provider is active)."""
    global _GENAI_MOD
    if _GENAI_MOD is None:
        from google import genai as _g

        _GENAI_MOD = _g
    return _GENAI_MOD


# ─── Error classification ─────────────────────────────────────────────

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


def is_non_retryable_llm_error(exc: Exception) -> bool:
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


def is_transient_llm_error(exc: Exception) -> bool:
    if is_non_retryable_llm_error(exc):
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


# ─── Client construction ──────────────────────────────────────────────


def _build_openai_client(*, base_url: str, api_key: str, model: str):
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError("pip install openai") from e
    # Ling Ling owns retries in ``LLMClient._complete_provider_text_with_retry``.
    # Leaving the SDK default (two hidden retries) multiplies the outer
    # three-attempt budget into as many as nine 300-second HTTP requests while
    # trace metadata still reports a single outer attempt.
    return OpenAI(base_url=base_url, api_key=api_key, timeout=300.0, max_retries=0), model


def _ollama_endpoint_reachable(base_url: str, timeout: float) -> bool:
    """Probe only the configured TCP endpoint; the real request validates Ollama."""
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _select_ollama_base_url(primary: str, fallback: str, probe_timeout: float) -> str:
    """Prefer the LAN endpoint and select the fallback when it is unreachable."""
    if not fallback or fallback == primary:
        return primary
    if _ollama_endpoint_reachable(primary, probe_timeout):
        logging.info("Ollama endpoint selected: primary %s", primary)
        return primary
    logging.warning(
        "Ollama primary endpoint %s is unreachable; using fallback %s",
        primary,
        fallback,
    )
    return fallback


def build_client(provider: str) -> tuple[Any, str]:
    """Build the (client, model) pair for a provider name."""
    if provider == "vllm":
        return _build_openai_client(
            base_url=os.getenv("VLLM_API_BASE", "http://192.168.1.103:9000/v1"),
            api_key=os.getenv("VLLM_API_KEY", "dummy-token"),
            model=os.getenv("VLLM_MODEL", "gpt-oss-20b"),
        )
    if provider == "ollama":
        primary = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
        fallback = os.getenv("OLLAMA_API_BASE_FALLBACK", "").strip()
        try:
            probe_timeout = max(float(os.getenv("OLLAMA_PROBE_TIMEOUT_SECONDS", "1.0")), 0.05)
        except ValueError:
            logging.warning("Invalid OLLAMA_PROBE_TIMEOUT_SECONDS; using 1.0 seconds")
            probe_timeout = 1.0
        return _build_openai_client(
            base_url=_select_ollama_base_url(primary, fallback, probe_timeout),
            api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
            model=os.getenv("OLLAMA_MODEL", "gemma2:27b"),
        )
    if provider == "gemini":
        client = _genai().Client(api_key=os.getenv("GEMINI_API_KEY"))
        return client, os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}. Expected one of: vllm, gemini, ollama.")


# ─── Completion ───────────────────────────────────────────────────────


def gemini_usage_counts(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None, None, None
    prompt = getattr(usage, "prompt_token_count", None)
    completion = getattr(usage, "candidates_token_count", None)
    total = getattr(usage, "total_token_count", None)
    return prompt, completion, total


def openai_chat(
    provider: str,
    client: Any,
    model: str,
    system_prompt: str,
    user_msg: Any,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> tuple[str, Any, dict[str, Any]]:
    extra_body = {"num_ctx": settings.MEMORY_LIMIT} if provider == "ollama" else {}
    reasoning_kwargs = (
        {"reasoning_effort": reasoning_effort} if provider == "ollama" and reasoning_effort else {}
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
        **reasoning_kwargs,
    )
    message = response.choices[0].message
    content = message.content or ""
    response_channel = "content"
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
            response_channel = "reasoning"
    choice = response.choices[0]
    return (
        content,
        getattr(response, "usage", None),
        {
            "response_channel": response_channel,
            "finish_reason": getattr(choice, "finish_reason", None),
        },
    )


def complete_once(
    provider: str,
    client: Any,
    model: str,
    system_prompt: str,
    user_msg: Any,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> tuple[str, int | None, int | None, int | None, dict[str, Any]]:
    """One completion call with response-channel provenance."""
    if provider == "gemini":
        genai = _genai()
        response = client.models.generate_content(
            model=model,
            contents=user_msg if isinstance(user_msg, list) else [str(user_msg)],
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        text = response.text or ""
        prompt_tokens, completion_tokens, total_tokens = gemini_usage_counts(response)
        provider_metadata = {
            "response_channel": "content",
            "finish_reason": getattr(response, "finish_reason", None),
        }
    else:
        text, usage, provider_metadata = openai_chat(
            provider,
            client,
            model,
            system_prompt,
            user_msg,
            temperature,
            max_tokens,
            reasoning_effort,
        )
        prompt_tokens, completion_tokens, total_tokens = usage_to_counts(usage)
    return text, prompt_tokens, completion_tokens, total_tokens, provider_metadata
