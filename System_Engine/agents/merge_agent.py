import logging
import json
import time
import shutil
from datetime import datetime
from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, WIKI_VAULT_DIR, INDEX_FILE, EXCALIDRAW_DIR, RAW_MERGED_DIR
from core.vault_utils import find_note, sanitize_filename


class MergeAgent(BaseAgent):
    def __init__(self, llm, rag=None):
        super().__init__(llm, rag)
        self.pages_dir = PAGES_DIR
        self.vault_dir = WIKI_VAULT_DIR
        self.index_file = INDEX_FILE
        self.excalidraw_dir = EXCALIDRAW_DIR

    def execute(self, task_context: dict) -> str:
        target_titles = task_context.get("target_titles", [])
        user_directive = task_context.get("user_directive", "")

        if len(target_titles) < 2:
            return "💧 合併失敗：至少需要 2 篇存在的筆記。"

        # 1. Gather Content
        valid_files = []
        combined_content = ""
        missing = []

        for title in target_titles:
            path = find_note(title)
            if path:
                valid_files.append(path)
                combined_content += f"\n\n# 原始檔案: {title}\n{path.read_text('utf-8')}"
            else:
                missing.append(title)

        if len(valid_files) < 2:
            return f"💧 合併失敗：實體找不到：{', '.join(missing)}"

        logging.info(f"Merging {len(valid_files)} documents...")

        # 2. Request LLM Synthesis
        system_base = self._load_prompt("system_base.md", required=True)
        agent_instruction = self._load_prompt("agent_merge.md", required=True)

        full_system_prompt = f"{system_base}\n\n{agent_instruction}"
        if user_directive:
            full_system_prompt += f"\n\n使用者特別指令：{user_directive}"

        index_content = self.index_file.read_text("utf-8") if self.index_file.exists() else ""

        # We use a specialized prompt for generating the entity page structure
        # Since LLMClient.generate_entity_page is already doing some heavy lifting,
        # we can either reuse it or implement a custom flow here.
        # Let's reuse the LLM wrapper's ability but ensure our BaseAgent stats are updated.

        # Note: We're calling llm.generate_entity_page which doesn't know about BaseAgent.stats.
        # In a real refactor, we'd make llm methods return stats or update them via callback.
        # For now, we'll manually estimate or let BaseAgent._write_report handle the output stats.

        result = self.llm.generate_entity_page(
            f"{combined_content}", "Merged_Note", index_content, context_hint=user_directive
        )

        if not result:
            return "💧 合併失敗：大語言模型未能生成有效的文章結構。"

        new_title = (
            result.get("title", "Merged_" + target_titles[0])
            .strip()
            .replace("/", "-")
            .replace("\\", "-")
        )
        tags = result.get("tags", [])
        page_type = result.get("type", "entity")
        body_content = result.get("content", "")
        excalidraw_diagrams = result.get("excalidraw_diagrams", [])

        # Process diagrams
        if excalidraw_diagrams:
            for diagram in excalidraw_diagrams:
                if hasattr(diagram, "model_dump"):
                    diagram = diagram.model_dump()
                filename = diagram.get("filename", f"diagram_{int(time.time())}").replace(
                    ".excalidraw", ""
                )
                excalidraw_path = self.excalidraw_dir / f"{filename}.excalidraw"
                with open(excalidraw_path, "w", encoding="utf-8") as ef:
                    json.dump(diagram.get("json_data", {}), ef, ensure_ascii=False, indent=2)
                diagram_link = f"![[Excalidraw/{filename}.excalidraw]]"
                if diagram_link not in body_content:
                    body_content += f"\n\n{diagram_link}"

        # 3. Final Content Healing (Invisible Repair)
        body_content = self._self_correct(body_content)

        # 4. Pick a merged page path that cannot overwrite or move a source note.
        ref_text = ", ".join([f"[[{f.stem}]]" for f in valid_files])
        final_body = f"{body_content}\n\n## 來源組合\n- 合併自: {ref_text}"
        source_paths = {f.resolve() for f in valid_files}
        new_title = sanitize_filename(new_title)  # math/path-sep safe stem + frontmatter title
        new_page_path = self.pages_dir / f"{new_title}.md"
        if new_page_path.resolve() in source_paths or new_page_path.exists():
            base_title = new_title
            suffix = 1
            while new_page_path.resolve() in source_paths or new_page_path.exists():
                suffix += 1
                new_title = f"{base_title} (Merged {suffix})"
                new_page_path = self.pages_dir / f"{new_title}.md"

        meta = {
            "title": new_title,
            "type": page_type,
            "tags": tags,
            "merged_from": [f.stem for f in valid_files],
            "merged_from_backup": [],
        }

        from core.parser import dump_markdown_with_metadata

        full_md = dump_markdown_with_metadata(meta, final_body)
        new_page_path.write_text(full_md, encoding="utf-8")

        if self.rag:
            self.rag.add_document(new_page_path, new_title, full_md, tags=tags)

        # 5. Archive originals to raw/merged/ only after the merged page is committed.
        archived_paths = []
        for filepath in valid_files:
            dest = RAW_MERGED_DIR / filepath.name
            if dest.exists():
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                dest = RAW_MERGED_DIR / f"{filepath.stem}_{timestamp}{filepath.suffix}"
            try:
                shutil.move(str(filepath), str(dest))
                # Record backup path relative to vault root
                archived_paths.append(str(dest.relative_to(WIKI_VAULT_DIR)))
                logging.info(f"Archived merged source: {filepath.name} -> {dest}")
                if self.rag:
                    self.rag.delete_document(filepath.stem)
            except Exception as e:
                logging.error(f"Failed to archive {filepath.name}: {e}")

        if archived_paths:
            meta["merged_from_backup"] = archived_paths
            full_md = dump_markdown_with_metadata(meta, final_body)
            new_page_path.write_text(full_md, encoding="utf-8")
            if self.rag:
                self.rag.add_document(new_page_path, new_title, full_md, tags=tags)

        return f"✅ 合併成功！\n- **新文章**：[[{new_title}]]\n- **原文已備份至** raw/merged/（可復原）\n- **來源**：{ref_text}"
