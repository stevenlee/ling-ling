import os
from pathlib import Path
from services.rag_manager import RAGManager
import logging

# Set logging to ERROR to keep output clean
logging.getLogger("chromadb").setLevel(logging.ERROR)

def inspect_db():
    try:
        manager = RAGManager()
        count = manager.collection.count()
        
        print("="*50)
        print("📁 ChromaDB 狀態報告")
        print("="*50)
        print(f"📍 資料庫路徑: {manager.db_dir}")
        print(f"📚 集合名稱: {manager.collection.name}")
        print(f"🔢 總 Chunk 數量: {count}")
        print("-" * 50)

        if count > 0:
            print("\n📌 抽樣預覽 (最新 3 筆)：")
            results = manager.collection.peek(3)
            
            for i in range(len(results['ids'])):
                item_id = results['ids'][i]
                metadata = results['metadatas'][i]
                doc_preview = results['documents'][i].replace('\n', ' ')[:120]
                
                print(f"\n🔹 ID: {item_id}")
                print(f"  └─ Title: {metadata.get('title')}")
                print(f"  └─ Tags:  {metadata.get('tags', 'None')}")
                print(f"  └─ Text:  {doc_preview}...")
        else:
            print("\n⚠️ 資料庫目前是空的。請執行 init_rag.py 進行索引。")
        
        print("\n" + "="*50)

    except Exception as e:
        print(f"❌ 無法讀取資料庫: {e}")

if __name__ == "__main__":
    inspect_db()
