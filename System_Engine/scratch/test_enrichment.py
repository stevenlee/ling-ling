import os
import sys
import logging
from pathlib import Path

# Setup paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.absolute()
sys.path.append(str(PROJECT_ROOT / "System_Engine"))

from core.tag_manager import TagManager
from core.config import TAG_MAP_FILE
from services.llm_client import LLMClient
from watchers.vault_watcher import VaultWatcher
from rag_manager import RAGManager

logging.basicConfig(level=logging.INFO)

def test_enrichment():
    # 1. Setup
    test_file = PROJECT_ROOT / "lings-desktop" / "pages" / "ScratchTest.md"
    test_file.write_text("---\ntags: [卷積神經網路]\n---\nTest content.", encoding='utf-8')
    
    rag = RAGManager()
    watcher = VaultWatcher(rag)
    
    print(f"Testing enrichment for: {test_file}")
    
    # 2. Run the process directly (bypassing the timer)
    watcher._process_modification(test_file, "ScratchTest")
    
    # 3. Check results
    print("\n--- RESULTS ---")
    if TAG_MAP_FILE.exists():
        print(f"TagMap.json created: {TAG_MAP_FILE.read_text()}")
    else:
        print("TagMap.json NOT created.")
    
    print(f"Updated File Content:\n{test_file.read_text()}")
    
    # Cleanup
    if test_file.exists(): test_file.unlink()

if __name__ == "__main__":
    test_enrichment()
