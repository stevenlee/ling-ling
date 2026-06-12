import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import json
from unittest.mock import MagicMock, patch
import pytest

from services.llm_client import LLMClient


@pytest.fixture
def llm():
    with patch("services.llm_client.LLM_PROVIDER", "vllm"):
        client = LLMClient()
        return client


from types import SimpleNamespace

def test_classify_document_reasoning_fallback(llm):
    fake_message = SimpleNamespace(content="", reasoning="patent", reasoning_content=None)
    fake_choice = SimpleNamespace(message=fake_message)
    fake_response = SimpleNamespace(choices=[fake_choice], usage=SimpleNamespace())
    
    llm.client = MagicMock()
    llm.client.chat.completions.create.return_value = fake_response
    
    result = llm.classify_document("doc.md", "content")
    assert result == "patent"


def test_select_profile_reasoning_fallback(llm):
    fake_message = SimpleNamespace(content="", reasoning=None, reasoning_content="academic")
    fake_choice = SimpleNamespace(message=fake_message)
    fake_response = SimpleNamespace(choices=[fake_choice], usage=SimpleNamespace())
    
    llm.client = MagicMock()
    llm.client.chat.completions.create.return_value = fake_response
    
    result = llm.select_profile(
        "doc.md", 
        "content", 
        [{"name": "academic", "hint": "..."}, {"name": "default", "hint": "..."}]
    )
    assert result == "academic"
