import os
import shutil
import sys
from pathlib import Path

# Add System_Engine to sys.path
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from services.rag_manager import RAGManager
from core.config import DATABASE_DIR, WIKI_VAULT_DIR
from core.parser import parse_markdown_metadata
import logging

def init_rag_from_scratch(wipe: bool = False):
    """
    Initializes the RAG database by indexing all markdown files in pages/ and Notes/.
    If wipe=True, wipes the existing collection first via ChromaDB API.
    """
    manager = RAGManager()
    
    if wipe:
        logging.info("💥 Wiping existing ChromaDB collection...")
        manager.wipe_collection()
            
    vault_dir = WIKI_VAULT_DIR
    
    if not vault_dir.exists():
        logging.warning("Vault directory does not exist! Nothing to index.")
        return
        
    logging.info(f"🚀 Starting to index knowledge base in {vault_dir}...")
    
    # 掃描範圍：只掃 pages/ 和 Notes/（白名單），使用 rglob 抓 nested 結構
    search_dirs = [
        vault_dir / "pages", 
        vault_dir / "Notes", 
    ]
    md_files = []
    for d in search_dirs:
        if d.exists():
            md_files.extend(list(d.rglob("*.md")))
    
    total = len(md_files)
    
    for i, filepath in enumerate(md_files, 1):
        try:
            # 排除一些特殊檔案
            if filepath.name.startswith('@ling-') or filepath.name == 'log.md':
                continue
                
            content = filepath.read_text(encoding='utf-8')
            meta = parse_markdown_metadata(content)
            tags = meta.get('tags', [])
            
            manager.add_document(filepath, filepath.stem, content, tags=tags)
            logging.info(f"[{i}/{total}] Indexed: {filepath.stem}")
        except Exception as e:
            logging.error(f"Failed to read/index {filepath.name}: {e}")
            
    logging.info(f"✨ RAG initialization complete! Total documents in DB: {manager.collection.count()}")

if __name__ == "__main__":
    import sys
    # 如果執行時帶有 --wipe 參數，則進行抹除
    wipe_it = "--wipe" in sys.argv
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    init_rag_from_scratch(wipe=wipe_it)
