import logging
import json
import time
from pathlib import Path

class MergeAgent:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.vault_dir = self.project_root / "lings-desktop"
        self.pages_dir = self.vault_dir / "pages"
        self.index_file = self.vault_dir / "index.md"
        self.excalidraw_dir = self.vault_dir / "Excalidraw"
        
    def _remove_from_index(self, title: str):
        if not self.index_file.exists():
            return
        content = self.index_file.read_text('utf-8')
        lines = [line for line in content.splitlines() if f"[[{title}]]" not in line]
        self.index_file.write_text("\n".join(lines) + "\n", 'utf-8')

    def merge_entities(self, target_titles: list[str], llm_wrapper, rag_manager, user_directive: str = None) -> str:
        """
        執行多個實體的合併程序。
        """
        # 1. 驗證檔案是否存在
        valid_files = []
        combined_content = ""
        missing = []
        
        for title in target_titles:
            filepath = None
            for directory in [self.pages_dir, self.vault_dir]:
                # Search recursively for the title
                p = next(directory.rglob(f"{title}.md"), None)
                if p:
                    filepath = p
                    break

            if filepath:
                valid_files.append(filepath)
                content = filepath.read_text('utf-8')
                combined_content += f"\n\n# 原始檔案: {title}\n{content}"
            else:
                missing.append(title)
                
        if len(valid_files) < 2:
            return f"❌ 合併失敗：至少需要 2 篇存在的筆記。以下實體找不到：{', '.join(missing)}"

        logging.info(f"Merging {len(valid_files)} documents...")

        # 2. 請求 LLM 進行重組生成
        system_instructions = "你現在是 Merge Agent，負責將以下的幾篇零散筆記，融合成一篇結構完整、不冗餘的 Markdown 筆記。請保留所有出現過的雙向連結與 Hashtags。"
        if user_directive:
            system_instructions += f"\n\n使用者特別指令：{user_directive}"
        index_content = self.index_file.read_text('utf-8') if self.index_file.exists() else ""
        
        # 利用現有的 generate_entity_page，它會輸出帶有標籤、內容的 JSON
        result = llm_wrapper.generate_entity_page(f"{system_instructions}\n\n{combined_content}", "Merged_Note", index_content)
        
        if not result:
            return "❌ 合併失敗：大語言模型未能生成有效的文章結構（產生了空值）。這通常是因為檔案過長超過了 Context Window，或是安全攔截。"
            
        new_title = result.get('title', "Merged_" + target_titles[0]).strip().replace("/", "-").replace("\\", "-")
        tags = result.get('tags', [])
        page_type = result.get('type', 'entity')
        body_content = result.get('content', '')
        excalidraw_diagrams = result.get('excalidraw_diagrams', [])
        
        # 處理圖表
        if excalidraw_diagrams:
            for diagram in excalidraw_diagrams:
                if hasattr(diagram, 'model_dump'):
                    diagram = diagram.model_dump()
                filename = diagram.get('filename', f"diagram_{int(time.time())}").replace('.excalidraw', '')
                excalidraw_path = self.excalidraw_dir / f"{filename}.excalidraw"
                with open(excalidraw_path, 'w', encoding='utf-8') as ef:
                    json.dump(diagram.get('json_data', {}), ef, ensure_ascii=False, indent=2)
                diagram_link = f"![[Excalidraw/{filename}.excalidraw]]"
                if diagram_link not in body_content:
                    body_content += f"\n\n{diagram_link}"
                    
        tags_formatted = f"[{', '.join(tags)}]" if tags else "[]"
        from datetime import datetime
        date_created = datetime.now().strftime("%Y-%m-%d")
        
        ref_text = ", ".join([f"[[{f.stem}]]" for f in valid_files])
        wiki_markdown = f"""---
title: {new_title}
type: {page_type}
date_created: {date_created}
tags: {tags_formatted}
---

{body_content}

## 來源組合
- 合併自: {ref_text}
"""
        
        # 3. 寫入新檔案
        new_page_path = self.pages_dir / f"{new_title}.md"
        with open(new_page_path, 'w', encoding='utf-8') as f:
            f.write(wiki_markdown)
            
        # 4. 註冊新 RAG
        rag_manager.add_document(new_page_path, new_title, wiki_markdown)
        
        # 5. 清理舊檔案與舊特徵
        for filepath in valid_files:
            old_title = filepath.stem
            # 刪檔案
            filepath.unlink()
            # 刪索引
            self._remove_from_index(old_title)
            # 刪 RAG
            rag_manager.delete_document(old_title)
            
        # 將新檔案加入索引 (利用 auto_ingest 原本邏輯會比較好，但能在這邊做較快)
        # 這裡借用一下 index_file append
        if not self._is_in_index(new_title):
            self._append_to_index(new_title)
            
        return f"✅ 合併手術成功！\n- **新文章**：[[{new_title}]]\n- **已銷毀舊文**：{ref_text}"

    def _is_in_index(self, title: str) -> bool:
        if not self.index_file.exists(): return False
        content = self.index_file.read_text('utf-8')
        return f"[[{title}]]" in content
        
    def _append_to_index(self, title: str):
        if not self.index_file.exists(): return
        content = self.index_file.read_text('utf-8')
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("## Entities"):
                lines.insert(i + 1, f"- [[{title}]]")
                break
        self.index_file.write_text("\n".join(lines) + "\n", 'utf-8')
