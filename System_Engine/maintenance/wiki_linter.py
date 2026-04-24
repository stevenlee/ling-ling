import re
from pathlib import Path
import logging
from core.parser import parse_markdown_metadata

class WikiLinter:
    def __init__(self, project_root: Path, rag_manager=None):
        self.project_root = project_root
        self.vault_dir = self.project_root / "lings-desktop"
        self.pages_dir = self.vault_dir / "pages"
        self.notes_dir = self.vault_dir / "Notes"
        self.index_file = self.vault_dir / "index.md"
        self.rag = rag_manager
        
    def scan_graph(self) -> dict:
        """
        掃描筆記實體地圖，回傳圖論分析結果。
        回傳: { 'broken_links': list, 'orphans': list }
        """
        existing_pages = set()
        linked_targets = set()
        
        # 1. 取得所有有效的頁面（Pages + Notes）
        for directory in [self.pages_dir, self.notes_dir]:
            if directory.exists():
                for filepath in directory.rglob("*.md"):
                    existing_pages.add(filepath.stem)
                    
                    # 2. 爬取該頁面內所有的 [[WikiLinks]]
                    content = filepath.read_text('utf-8')
                    links = re.findall(r'\[\[(.*?)\]\]', content)
                    for link in links:
                        clean_link = link.split('|')[0].strip() # 處理可能的別名 [[Target|Alias]]
                        # 排除 Excalidraw 圖檔，避免被判定為 Markdown 死連結
                        if not clean_link.startswith("Excalidraw/") and not clean_link.endswith(".excalidraw"):
                            # Normalize link: Use stem to match existing_pages which only contains stems
                            normalized_target = Path(clean_link).stem
                            linked_targets.add(normalized_target)
                            
        # 3. 額外掃描 index.md 內的連結 (主目錄)
        if self.index_file.exists():
            content = self.index_file.read_text('utf-8')
            links = re.findall(r'\[\[(.*?)\]\]', content)
            for link in links:
                clean_link = link.split('|')[0].strip()
                if not clean_link.startswith("Excalidraw/") and not clean_link.endswith(".excalidraw"):
                    normalized_target = Path(clean_link).stem
                    linked_targets.add(normalized_target)
                    
        # 死連結：被引用了，但該實體 (.md) 不存在
        broken_links = linked_targets - existing_pages
        # 排除外部或是特殊保留字 (若有的話)
        
        # 孤兒頁面：實際存在，但是「沒有被任何其他實體頁面引用」
        # （不把 index.md 內的主目錄引用算進去，因為那是全域索引）
        orphans = existing_pages - linked_targets
        
        return {
            "broken_links": list(broken_links),
            "orphans": list(orphans),
            "total_pages": len(existing_pages),
            "pages_list": existing_pages
        }

    def scan_rag_health(self) -> dict:
        """
        掃描向量資料庫與本地檔案的同步狀況。
        """
        if not self.rag:
            return {"error": "RAGManager not initialized"}

        # 1. 取得磁碟上的實體清單
        existing_pages = set()
        for directory in [self.pages_dir, self.notes_dir]:
            if directory.exists():
                for filepath in directory.rglob("*.md"):
                    existing_pages.add(filepath.stem)

        # 2. 取得資料庫中的實體清單
        rag_titles = self.rag.get_all_indexed_titles()

        # 3. 比較差異
        unindexed = existing_pages - rag_titles
        stale = rag_titles - existing_pages
        total_chunks = self.rag.get_total_chunks_count()

        return {
            "unindexed_files": list(unindexed),
            "stale_entries": list(stale),
            "total_chunks": total_chunks,
            "total_indexed_files": len(rag_titles)
        }

    def perform_repair(self) -> str:
        """
        執行自動修復：刪除過時資料並重新索引缺失檔案。
        """
        if not self.rag:
            return "❌ 無法修復：RAGManager 未啟動。"

        health = self.scan_rag_health()
        actions = []

        # 1. 清理過時資料 (Zombie Cleanup)
        for title in health['stale_entries']:
            self.rag.delete_document(title)
            actions.append(f"✅ 已刪除過時索引: {title}")

        # 2. 補齊缺失索引 (Missing indexing)
        for title in health['unindexed_files']:
            # 尋找檔案路徑
            filepath = None
            for directory in [self.pages_dir, self.notes_dir]:
                # Recursive search for the title
                p = next(directory.rglob(f"{title}.md"), None)
                if p:
                    filepath = p
                    break
            
            if filepath:
                try:
                    content = filepath.read_text(encoding='utf-8')
                    meta = parse_markdown_metadata(content)
                    tags = meta.get('tags', [])
                    # We can't easily get chunk count without calling _chunk_text or watching rag_manager logs
                    # but RAGManager.add_document knows it. Let's adjust it to return info or just re-calculate here.
                    chunks = self.rag._chunk_text(content)
                    self.rag.add_document(filepath, title, content, tags=tags)
                    actions.append(f"✅ 已補齊缺失索引: **{title}** ({len(chunks)} chunks, 標籤: {', '.join(tags) if tags else '無'})")
                except Exception as e:
                    actions.append(f"❌ 索引失敗 {title}: {e}")

        if not actions:
            summary_actions = ["🎉 資料庫狀態健康，目前無需維護。"]
        else:
            summary_actions = actions

        # 3. 獲取最終統計數據
        stats = self.scan_rag_health()
        
        # 計算資料庫檔案大小
        db_size_bytes = 0
        db_path = self.project_root / "lings-desktop" / "Database" / "chroma_db"
        if db_path.exists():
            for f in db_path.rglob('*'):
                if f.is_file():
                    db_size_bytes += f.stat().st_size
        
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        report_body = "\n".join(summary_actions)
        
        stats_summary = f"""---
### 📊 向量資料庫維護統計
- **總計索引檔案 (Files)**: {stats['total_indexed_files']}
- **總計向量分區 (Chunks)**: {stats['total_chunks']}
- **資料庫佔用空間**: {db_size_bytes / 1024 / 1024:.2f} MB
- **最後維護時間**: {now_str}
"""
        return f"{report_body}\n\n{stats_summary}"


    def generate_report(self, llm_wrapper) -> str:
        """
        執行多階段巡邏，並產出綜合報告。
        """
        # 第一階段：冷靜的 Python 圖論演算法
        graph_data = self.scan_graph()
        
        broken_text = "\n".join([f"- [[{b}]]" for b in graph_data['broken_links']]) if graph_data['broken_links'] else "- 🎉 沒有死連結！"
        orphan_text = "\n".join([f"- [[{o}]]" for o in graph_data['orphans']]) if graph_data['orphans'] else "- 🎉 沒有孤兒頁面！大家都互相認識。"
        
        # 第二階段：大腦語意分析
        index_content = self.index_file.read_text('utf-8') if self.index_file.exists() else "目前沒有實體紀錄。"
        
        prompt = f"""請以「知識庫健康檢查醫生」的身分，幫我檢查這個 Wiki 的目錄與實體之間，是否有語意上的「冗餘」或「矛盾」。

以下是知識庫目前的所有實體清單（index.md）：
{index_content}

任務：
1. 找出「名稱高度相似、可能是同一個概念重複建立」的實體（Redundancies），並列出建議合併的組合。例如 '蘋果' 與 'Apple' 或 'Agentic Workflow' 與 'Agentic Workflows'。
2. 針對上述分析提出 1-2 句話的洞察建議。
3. 語氣請保持幽默輕快。

請直接使用 Markdown 格式輸出，不要被 JSON 格式侷限。
"""
        # 利用我們先前建立的純文本提問函數
        llm_analysis = llm_wrapper.answer_query(prompt, wiki_context="")
        
        # 第三階段：向量資料庫健康度 (純演算法)
        rag_health = self.scan_rag_health()
        if "error" in rag_health:
            rag_text = f"⚠️ {rag_health['error']}"
        else:
            unindexed_text = "\n".join([f"- {f}.md" for f in rag_health['unindexed_files']]) if rag_health['unindexed_files'] else "- 🎉 所有檔案皆已索引"
            stale_text = "\n".join([f"- {s}" for s in rag_health['stale_entries']]) if rag_health['stale_entries'] else "- 🎉 沒有過時的殘留資料"
            
            rag_text = f"""- **總索引 Chunk 數量**: {rag_health['total_chunks']}
- **已索引實體數量**: {rag_health['total_indexed_files']}
- **待索引檔案 (Unindexed)**:
{unindexed_text}
- **過時殘留索引 (Stale)**:
{stale_text}"""


        # 組合報告
        report = f"""# 🌸 小花園週報
        
## 🧹大掃除時間

### Broken Links
*被其他筆記提起，但你還沒寫出這篇筆記實體的名稱：*
{broken_text}

### Orphan Pages
*已經寫好了，但卻被邊緣化、從來沒有被其他筆記連過去的頁面：*
{orphan_text}

---

## 🍵抹茶時間

{llm_analysis}

---

## 🫧✨ 亮晶晶展示櫃同步中

{rag_text}

---
*此報告由 Wiki Linter 自動生成。*
"""
        return report

# 簡單測試用
if __name__ == "__main__":
    linter = WikiLinter(Path(__file__).parent.parent.parent.absolute())
    print(linter.scan_graph())
