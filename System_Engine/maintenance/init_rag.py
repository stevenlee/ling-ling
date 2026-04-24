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
    Initializes the RAG database by indexing all markdown files in the pages directory.
    If wipe=True, deletes the existing database first.
    """
    if wipe:
        db_path = DATABASE_DIR / "chroma_db"
        if db_path.exists():
            logging.info(f"💥 Wiping existing ChromaDB at {db_path}...")
            shutil.rmtree(db_path)
            
    manager = RAGManager()
    vault_dir = WIKI_VAULT_DIR
    
    if not vault_dir.exists():
        logging.warning("Vault directory does not exist! Nothing to index.")
        return
        
    logging.info(f"🚀 Starting to index knowledge base in {vault_dir}...")
    
    # 掃描範圍：根目錄、pages、Notes、以及穩定的 raw/clippings
    search_dirs = [
        vault_dir, 
        vault_dir / "pages", 
        vault_dir / "Notes", 
        vault_dir / "raw" / "clippings"
    ]
    md_files = []
    for d in search_dirs:
        if d.exists():
            # 只抓取該層目錄的 .md，不遞迴（避免抓到備份或 raw）
            md_files.extend(list(d.glob("*.md")))
    
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
