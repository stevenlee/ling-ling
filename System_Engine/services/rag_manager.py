import os
from pathlib import Path
import chromadb
from chromadb.utils import embedding_functions
import logging
import time
from functools import wraps

def retry_on_db_lock(retries=3, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "database is locked" in str(e).lower() or "timeout" in str(e).lower():
                        logging.warning(f"Database locked, retrying {i+1}/{retries}...")
                        time.sleep(delay * (i + 1))
                        last_err = e
                    else:
                        raise e
            raise last_err
        return wrapper
    return decorator

class RAGManager:
    def __init__(self, db_path: str = None):
        from core.config import DATABASE_DIR
        # If db_path is provided, use it as a subfolder, otherwise use DATABASE_DIR
        self.db_dir = (DATABASE_DIR / db_path) if db_path else DATABASE_DIR
        self.db_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize ChromaDB persistent client
        self.client = chromadb.PersistentClient(path=str(self.db_dir))
        
        # Use simple default ONNX embedding model to keep things local and PyTorch-free
        # Default is all-MiniLM-L6-v2 which is great given its size.
        self.ef = embedding_functions.DefaultEmbeddingFunction()
        
        # Collection for wiki pages
        self.collection = self.client.get_or_create_collection(
            name="wiki_pages",
            embedding_function=self.ef
        )

    def _chunk_text(self, text: str, max_chunk_size: int = 1000, overlap: int = 200):
        """
        Simple text chunker based on character count with overlap.
        In production, recursive character splitting or markdown-header splitting is better.
        """
        chunks = []
        start = 0
        text_length = len(text)
        
        while start < text_length:
            end = start + max_chunk_size
            
            # If we're not at the very end, try to find a natural break (like double newline)
            if end < text_length:
                break_point = text.rfind("\n\n", start, end)
                if break_point != -1 and break_point > start + (max_chunk_size // 2):
                    end = break_point + 2
                else:
                    # Fallback to single newline
                    break_point = text.rfind("\n", start, end)
                    if break_point != -1 and break_point > start + (max_chunk_size // 2):
                        end = break_point + 1
                        
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
                
            start = end - overlap
            
        return chunks

    def add_document(
        self,
        filepath: Path,
        title: str,
        text: str,
        tags: list[str] = None,
        section_path: list[str] | None = None,
    ):
        """
        Chunk and add a markdown document to the ChromaDB.

        `section_path` (ThoughtfulSplitter P4) is the heading hierarchy this
        document/part lives under, e.g. ["Chapter 1", "Background"]. It's
        encoded into chunk metadata as `>chapter 1>background>` so RAG
        queries can structurally filter by section.
        """
        import time
        from datetime import datetime
        try:
            # 1. Clean up stale chunks for this title first (Zombie Prevention)
            self.delete_document(title)

            timestamp = datetime.now().isoformat()
            chunks = self._chunk_text(text)
            if not chunks:
                return

            ids = []
            documents = []
            metadatas = []
            # Lowercase + `>...>` so ChromaDB `where` clauses can use
            # `$contains: ">background>"` to find content in that section.
            section_marker = (
                ">" + ">".join(s.lower().strip() for s in section_path) + ">"
                if section_path else ""
            )

            for i, chunk in enumerate(chunks):
                ids.append(f"{title}_chunk_{i}")
                documents.append(chunk)
                meta = {
                    "source": filepath.name,
                    "title": title,
                    "chunk_index": i,
                    "timestamp": timestamp,
                    "tags": f",{','.join(tags)}," if tags else "",
                    "section_path": section_marker,
                }
                metadatas.append(meta)

            # Upsert overwrites chunks with same ID if they are updated
            self._upsert_with_retry(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            logging.info(f"Added '{title}' to RAG DB ({len(chunks)} chunks)")
        except Exception as e:
            logging.error(f"Failed to add document '{title}' to RAG: {e}")

    def query_similar_notes(self, query_text: str, top_k: int = 3) -> list[str]:
        """
        Search for most relevant chunks.
        Returns a list of markdown formatted strings.
        """
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=top_k
            )
            
            documents = results.get('documents', [[]])[0]
            metadatas = results.get('metadatas', [[]])[0]
            
            context_pieces = []
            for i, doc in enumerate(documents):
                source_title = metadatas[i].get('title', 'Unknown Source') if metadatas and i < len(metadatas) else 'Unknown Source'
                context_pieces.append(f"### [來自筆記: {source_title}]\n{doc}")
                
            return context_pieces
            
        except Exception as e:
            logging.error(f"RAG query failed: {e}")
            return []

    @retry_on_db_lock()
    def _upsert_with_retry(self, **kwargs):
        self.collection.upsert(**kwargs)

    @retry_on_db_lock()
    def delete_document(self, title: str):
        """
        Delete all chunks associated with a specific document title.
        """
        try:
            self.collection.delete(where={"title": title})
            logging.info(f"Deleted '{title}' from RAG DB")
        except Exception as e:
            if "not found" in str(e).lower(): # Handle case where title doesn't exist yet
                return
            logging.error(f"Failed to delete document '{title}' from RAG: {e}")

    def get_all_indexed_titles(self) -> set:
        """
        Retrieves a set of all unique document titles currently in the database.
        """
        try:
            results = self.collection.get(include=['metadatas'])
            metadatas = results.get('metadatas', [])
            titles = set(m.get('title') for m in metadatas if m and 'title' in m)
            return titles
        except Exception as e:
            logging.error(f"Failed to get indexed titles: {e}")
            return set()

    def get_total_chunks_count(self) -> int:
        """
        Returns the total number of chunks in the collection.
        """
        try:
            return self.collection.count()
        except Exception as e:
            logging.error(f"Failed to count chunks: {e}")
            return 0

    def wipe_collection(self):
        """
        Completely wipes the wiki_pages collection.
        """
        try:
            logging.warning("RAGManager: Wiping 'wiki_pages' collection...")
            self.client.delete_collection("wiki_pages")
            self.collection = self.client.get_or_create_collection(
                name="wiki_pages",
                embedding_function=self.ef
            )
            logging.info("RAGManager: Collection wiped and recreated.")
        except Exception as e:
            logging.error(f"RAGManager: Failed to wipe collection: {e}")

if __name__ == "__main__":
    # Test initialization
    manager = RAGManager()
    print(f"RAG Manager initialized. Collection count: {manager.collection.count()}")
