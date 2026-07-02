import logging
from agents.base_agent import BaseAgent
from core.config import TAG_MAP_FILE, PAGES_DIR, NOTES_DIR, COMMAND_PREFIX
from core.parser import parse_markdown_metadata
from core.tag_manager import TagManager


class TagPatrolAgent(BaseAgent):
    def __init__(self, llm, rag=None):
        super().__init__(llm, rag)
        self.tm = TagManager(TAG_MAP_FILE)
        self.pages_dir = PAGES_DIR
        self.notes_dir = NOTES_DIR
        self.report_file_name = f"{COMMAND_PREFIX}repair-tags.md"

    def audit_tags(self) -> dict:
        """
        Scans all files and groups issues by (bad, good, reason).
        Returns: { (bad, good, reason): [list_of_filepaths] }
        """
        grouped_issues = {}
        for directory in [self.pages_dir, self.notes_dir]:
            if not directory.exists():
                continue
            for filepath in directory.rglob("*.md"):
                try:
                    content = filepath.read_text("utf-8")
                    meta = parse_markdown_metadata(content)
                    tags = meta.get("tags", [])

                    normalized_tags = [self.tm.normalize(t) for t in tags]

                    # 1. Format check
                    for i, tag in enumerate(tags):
                        if tag != normalized_tags[i]:
                            key = (tag, normalized_tags[i], "format_error")
                            if key not in grouped_issues:
                                grouped_issues[key] = []
                            grouped_issues[key].append(filepath)

                    # 2. Bilingual check
                    for tag in normalized_tags:
                        if self.tm.is_bilingual_needed(tag):
                            eq = self.tm.get_equivalent(tag)
                            if eq:
                                eq_norm = self.tm.normalize(eq)
                                if eq_norm not in normalized_tags:
                                    key = (tag, eq_norm, "missing_pair")
                                    if key not in grouped_issues:
                                        grouped_issues[key] = []
                                    grouped_issues[key].append(filepath)
                            else:
                                key = (tag, "[AUTO-LEARN]", "missing_pair")
                                if key not in grouped_issues:
                                    grouped_issues[key] = []
                                grouped_issues[key].append(filepath)
                except Exception as e:
                    logging.error(f"TagPatrol: Failed to audit {filepath.name}: {e}")
        return grouped_issues

    def execute(self, task_context: dict = None) -> str:
        logging.info("✂️✨ TagPatrolAgent: Starting audit...")
        grouped_issues = self.audit_tags()

        # Load Instructions for stats/prompt tracking
        self._load_prompt("agent_tag_patrol.md")

        if not grouped_issues:
            content = "🎉 太棒了！目前全庫標籤都符合規範，沒有發現問題。\n"
            self._write_report("Tags", content, "sys")
            return content

        rows = [
            "> [!TIP]\n> **✂️✨ 標籤修復指令**：請在下方勾選 `- [x]` 並將此檔案拖入 `toLingLing/` 資料夾即可執行批量修復。\n"
        ]
        rows.append("### 📋 待處理修復清單 (已彙整)\n")

        for (bad, good, issue_type), filepaths in grouped_issues.items():
            reason = "修正格式" if issue_type == "format_error" else "補齊英文對照"
            affected_links = ", ".join(
                [f"[[{f.stem}]]" for f in filepaths[:10]]
            )  # Limit display to 10 files
            if len(filepaths) > 10:
                affected_links += f" ... 等共 {len(filepaths)} 個檔案"

            paths_str = ";".join([str(f.absolute()) for f in filepaths])
            rows.append(
                f"- [ ] {reason}: `{bad}` -> `{good}` (影響: {affected_links}) | PATHS: `{paths_str}`"
            )

        content = "\n".join(rows)
        self._write_report("Tags", content, "sys")
        return content


if __name__ == "__main__":
    # For standalone testing
    from services.llm_client import LLMClient

    llm = LLMClient()
    agent = TagPatrolAgent(llm)
    agent.execute()
