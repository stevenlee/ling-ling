import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.tag_manager import TagManager
from services.tag_optimizer import TagOptimizer

class MockLLM:
    def __init__(self, generated_tags):
        self.generated_tags = generated_tags
        
    def generate_structured(self, prompt, schema):
        return {"tags": self.generated_tags}

def test_move_cjk_to_aliases():
    tags = ["machine-learning", "深度學習", "python", "神經網絡"]
    current_aliases = ["ai"]
    
    new_tags, new_aliases = TagManager.move_cjk_to_aliases(tags, current_aliases)
    
    assert new_tags == ["machine-learning", "python"]
    assert "深度學習" in new_aliases
    assert "神經網絡" in new_aliases
    assert "ai" in new_aliases
    assert len(new_aliases) == 3

def test_tag_optimizer_find_best_matches(tmp_path):
    map_file = tmp_path / "tagScrapbook.md"
    map_file.write_text("---\nmachine learning: ml\n---\n# tags")
    
    optimizer = TagOptimizer(MockLLM([]))
    optimizer.tag_manager.mapping_file = map_file
    optimizer.tag_manager.load()
    
    # "machine learning" should be mapped to "ml"
    # "深度學習" should map to nothing (kept as is)
    keywords = ["machine learning", "深度學習", "new-tag"]
    final = optimizer._find_best_matches(keywords)
    
    assert "ml" in final
    assert "machine-learning" in final  # It gets added back as alias!
    assert "深度學習" in final
    assert "new-tag" in final
