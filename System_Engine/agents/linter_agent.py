import logging
import re
from pathlib import Path
from datetime import datetime
from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, NOTES_DIR, INDEX_FILE, WIKI_VAULT_DIR, PROJECT_ROOT, settings
from core.parser import parse_markdown_metadata

class LinterAgent(BaseAgent):
    def __init__(self, llm, rag_manager):
        super().__init__(llm, rag_manager)
        self.vault_dir = WIKI_VAULT_DIR
        self.pages_dir = PAGES_DIR
        self.notes_dir = NOTES_DIR
        self.index_file = INDEX_FILE

    def scan_graph(self) -> dict:
        existing_pages = set()
        linked_targets = set()
        
        for directory in [self.pages_dir, self.notes_dir]:
            if directory.exists():
                for filepath in directory.rglob("*.md"):
                    existing_pages.add(filepath.stem)
                    content = filepath.read_text('utf-8')
                    links = re.findall(r'\[\[(.*?)\]\]', content)
                    for link in links:
                        clean_link = link.split('|')[0].strip()
                        if not clean_link.startswith("Excalidraw/") and not clean_link.endswith(".excalidraw"):
                            normalized_target = Path(clean_link).stem
                            linked_targets.add(normalized_target)
                            
        broken_links = linked_targets - existing_pages
        orphans = existing_pages - linked_targets
        
        return {
            "broken_links": list(broken_links),
            "orphans": list(orphans),
            "total_pages": len(existing_pages)
        }

    def scan_rag_health(self) -> dict:
        if not self.rag:
            return {"error": "RAGManager not initialized"}

        existing_pages = set()
        for directory in [self.pages_dir, self.notes_dir]:
            if directory.exists():
                for filepath in directory.rglob("*.md"):
                    existing_pages.add(filepath.stem)

        rag_titles = set()
        indexed_sources = set()
        title_sources = {}
        try:
            results = self.rag.collection.get(include=['metadatas'])
            for metadata in results.get('metadatas', []):
                if not metadata:
                    continue
                title = metadata.get('title')
                if title:
                    rag_titles.add(title)
                if metadata.get('source'):
                    source_stem = Path(metadata['source']).stem
                    indexed_sources.add(source_stem)
                    if title:
                        title_sources.setdefault(title, set()).add(source_stem)
        except Exception as e:
            logging.error(f"Failed to scan RAG metadata: {e}")
            rag_titles = self.rag.get_all_indexed_titles()

        indexed_names = rag_titles | indexed_sources
        unindexed = existing_pages - indexed_names
        stale = {
            title for title in rag_titles
            if title not in existing_pages and not (title_sources.get(title, set()) & existing_pages)
        }
        total_chunks = self.rag.get_total_chunks_count()

        return {
            "unindexed_files": list(unindexed),
            "stale_entries": list(stale),
            "total_chunks": total_chunks,
            "total_indexed_files": len(rag_titles)
        }

    def perform_repair(self) -> str:
        if not self.rag:
            return "💧 無法修復：RAGManager 未啟動。"

        health = self.scan_rag_health()
        actions = []

        for title in health['stale_entries']:
            self.rag.delete_document(title)
            actions.append(f"✅ 已刪除過時索引: {title}")

        for title in health['unindexed_files']:
            filepath = None
            for directory in [self.pages_dir, self.notes_dir]:
                p = next(directory.rglob(f"{title}.md"), None)
                if p:
                    filepath = p
                    break
            
            if filepath:
                try:
                    content = filepath.read_text(encoding='utf-8')
                    meta = parse_markdown_metadata(content)
                    tags = meta.get('tags', [])
                    self.rag.add_document(filepath, title, content, tags=tags)
                    actions.append(f"✅ 已補齊缺失索引: **{title}**")
                except Exception as e:
                    actions.append(f"💧 索引失敗 {title}: {e}")

        return "\n".join(actions) if actions else "🎉 資料庫狀態健康，目前無需維護。"

    def execute(self, task_context: dict) -> str:
        do_repair = task_context.get('repair', settings.SELF_HEALING)
        
        # 1. Structural Scan
        graph_data = self.scan_graph()
        broken_text = "\n".join([f"- [[{b}]]" for b in graph_data['broken_links']]) if graph_data['broken_links'] else "- 🎉 沒有死連結！"
        orphan_text = "\n".join([f"- [[{o}]]" for o in graph_data['orphans']]) if graph_data['orphans'] else "- 🎉 沒有孤兒頁面！"
        
        # 2. Semantic Analysis
        index_content = self.index_file.read_text('utf-8') if self.index_file.exists() else "目前沒有實體紀錄。"
        system_base = self._load_prompt("system_base.md")
        agent_instruction = self._load_prompt("agent_linter.md")
        
        prompt = f"""
{system_base}

{agent_instruction}

以下是目前知識庫的實體清單：
{index_content}
"""
        llm_analysis = self.llm.answer_query("請執行語意巡邏，找出冗餘與提供洞察建議。", wiki_context="", custom_instruction=prompt)
        llm_analysis = self._self_correct(llm_analysis)
        
        # 3. RAG Health
        rag_health = self.scan_rag_health()
        rag_text = ""
        if "error" in rag_health:
            rag_text = f"💦 {rag_health['error']}"
        else:
            unindexed_text = "\n".join([f"- {f}.md" for f in rag_health['unindexed_files']]) if rag_health['unindexed_files'] else "- 🎉 所有檔案皆已索引"
            stale_text = "\n".join([f"- {s}" for s in rag_health['stale_entries']]) if rag_health['stale_entries'] else "- 🎉 沒有過時的殘留資料"
            rag_text = f"""- **總索引 Chunk 數量**: {rag_health['total_chunks']}
- **待索引檔案**:
{unindexed_text}
- **過時殘留索引**:
{stale_text}"""

        # 4. Repair if requested
        repair_summary = self.perform_repair() if do_repair else ""

        # 5. Build Report
        report_body = f"""# 🌸 小花園週報
        
## 🧹大掃除時間
### Broken Links
{broken_text}

### Orphan Pages
{orphan_text}

---

## 🍵抹茶時間
{llm_analysis}

---

## 🫧✨ 亮晶晶展示櫃同步中
{rag_text}

{f'### 🛠️ 自動修復紀錄' + chr(10) + repair_summary if repair_summary else ''}
"""
        self._write_report("巡邏報告", report_body, "report_patrol")
        return report_body
