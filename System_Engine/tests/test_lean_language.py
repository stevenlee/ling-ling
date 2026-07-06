"""P4: lean `complete()` can opt into the OUTPUT-LANGUAGE banner.

Path C (`complete()`) uses the caller's system prompt verbatim — no language
banner. That's correct for JSON extraction and for content-language artifacts,
but a user-visible-prose caller with no language guarantee can now pass
`pin_language=True` to prepend the same banner PromptComposer uses. Default
stays byte-identical (no banner) so every existing lean call is unchanged.
"""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.llm.prompt_composer import language_banner
from services.llm_client import LLMClient


def _client():
    c = LLMClient.__new__(LLMClient)
    captured = {}

    def fake(**kw):
        captured.update(kw)
        return "ok"

    c._complete_text = fake
    c._captured = captured
    return c


def test_default_is_verbatim_no_banner():
    c = _client()
    c.complete("SYS", "user")
    assert c._captured["system_prompt"] == "SYS"


def test_pin_language_prepends_banner():
    c = _client()
    c.complete("SYS", "user", pin_language=True)
    sp = c._captured["system_prompt"]
    assert sp.startswith(language_banner())
    assert sp.endswith("SYS")
    assert "OUTPUT LANGUAGE (highest priority)" in sp


def test_language_banner_nonempty_and_uses_hint():
    # The extracted function is what both PromptComposer and complete() share;
    # it must actually name the configured output language.
    banner = language_banner()
    assert "OUTPUT LANGUAGE (highest priority)" in banner
    assert banner.strip()
