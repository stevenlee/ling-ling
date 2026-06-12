"""Batch-2 T3: select_profile menu fix + single-name salvage parsing."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from unittest.mock import MagicMock, patch
import pytest

from services.llm_client import LLMClient


OPTIONS = [
    {"name": "academic", "hint": "For research papers"},
    {"name": "code", "hint": "For programming tutorials"},
]


@pytest.fixture
def llm():
    with patch("services.llm_client.LLM_PROVIDER", "gemini"):
        with patch("services.llm_client._genai", MagicMock()):
            client = LLMClient()
            client._complete_text = MagicMock()
            return client


def _select(llm, answer):
    llm._complete_text.return_value = answer
    return llm.select_profile("doc.md", "content", OPTIONS)


def test_exact_name_passthrough(llm):
    assert _select(llm, "academic") == "academic"


def test_menu_shows_profile_names(llm):
    _select(llm, "academic")
    system_prompt = llm._complete_text.call_args.kwargs["system_prompt"]
    assert "- academic: For research papers" in system_prompt
    assert "- code: For programming tutorials" in system_prompt


def test_salvage_name_wrapped_in_prose(llm):
    assert _select(llm, "I choose academic.") == "academic"


def test_two_names_in_answer_is_none(llm):
    assert _select(llm, "either academic or code") == "none"


def test_hint_text_without_name_is_none(llm):
    assert _select(llm, "programming tutorials") == "none"


def test_explicit_none_stays_none(llm):
    assert _select(llm, "none") == "none"


def test_empty_options_short_circuits(llm):
    assert llm.select_profile("doc.md", "content", []) == "none"
    llm._complete_text.assert_not_called()
