import logging
import re
from datetime import datetime
from pathlib import Path

from core.config import FROM_LLM_DIR, PROMPTS_DIR
from core.parser import (
    MERMAID_START_RE,
    clean_llm_response,
    dump_markdown_with_metadata,
    run_markdown_quality_checks,
)
from core.utils import MtimeCache

# Shared across all agent instances so a multi-strategy run only re-reads each
# prompt template once per session (auto-invalidates on edit).
_PROMPT_CACHE = MtimeCache()

# Fenced mermaid block: capture the inner code so we can repair it in-place
# while preserving the surrounding text exactly (str.replace is fragile when
# two identical broken blocks appear in one document).
_MERMAID_BLOCK_RE = re.compile(r'```mermaid\n(.*?)\n```', re.DOTALL)

# Cheap signal that the body contains raw (un-fenced) mermaid code.
_MERMAID_KEYWORDS = (
    "graph TD", "graph LR", "graph TB", "graph BT", "graph RL",
    "flowchart TD", "flowchart LR", "flowchart TB", "flowchart BT", "flowchart RL",
    "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2",
    "erDiagram", "gantt", "pie", "mindmap", "timeline", "journey",
)

# Strip out mermaid `%%` comments and double-quoted labels before any
# structural inspection: counting brackets in those regions is meaningless.
_MERMAID_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_MERMAID_COMMENT_RE = re.compile(r'%%[^\n]*')


class BaseAgent:
    def __init__(self, llm, rag=None):
        self.llm = llm
        self.rag = rag
        self.stats = {"input_chars": 0, "output_chars": 0}

    def _load_prompt(self, prompt_name: str) -> str:
        if not prompt_name.endswith(".md"):
            prompt_name += ".md"
        prompt_path = PROMPTS_DIR / prompt_name
        content = _PROMPT_CACHE.read(prompt_path)
        if not content and not prompt_path.exists():
            logging.warning(f"Prompt template not found: {prompt_name}")
            return ""
        self.stats["input_chars"] += len(content)
        return content

    def _load_mermaid_rules(self) -> str:
        return self._load_prompt("mermaid_rules.md")

    def _self_correct(self, content: str) -> str:
        """Invisible healing for LLM output: unwrap, repair mermaid, normalize."""
        if not content:
            return content

        content = clean_llm_response(content)

        if "```mermaid" not in content and self._looks_like_bare_mermaid(content):
            logging.info("Detected raw Mermaid code without backticks. Wrapping.")
            content = f"```mermaid\n{content.strip()}\n```"

        if "```mermaid" in content:
            content = self._heal_mermaid_blocks(content)

        content, fixes = run_markdown_quality_checks(content)
        if fixes:
            logging.info(f"Applied markdown quality fixes: {', '.join(fixes)}")
        return content

    @staticmethod
    def _looks_like_bare_mermaid(content: str) -> bool:
        """True if the body is *only* an unwrapped mermaid diagram."""
        stripped = content.strip()
        if not stripped or stripped.startswith("```"):
            return False
        if not any(kw in stripped for kw in _MERMAID_KEYWORDS):
            return False
        first_line = next((ln for ln in stripped.splitlines() if ln.strip()), "")
        return bool(MERMAID_START_RE.match(first_line))

    def _heal_mermaid_blocks(self, content: str) -> str:
        """Repair each fenced mermaid block in `content` via LLM when broken.

        Substitution is done by character offset so identical blocks don't
        collide, and an LLM repair that still looks broken is rejected.
        """
        pieces: list[str] = []
        cursor = 0
        for match in _MERMAID_BLOCK_RE.finditer(content):
            pieces.append(content[cursor:match.start()])
            block = match.group(1)
            if self._is_mermaid_broken(block):
                logging.info("Detected potentially broken Mermaid block. Attempting self-correction.")
                repaired = self._llm_repair_mermaid(block)
                if repaired and not self._is_mermaid_broken(repaired):
                    pieces.append(f"```mermaid\n{repaired}\n```")
                    cursor = match.end()
                    continue
                logging.warning("Mermaid self-correction rejected (still broken or empty); keeping original.")
            pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(content[cursor:])
        return "".join(pieces)

    def _llm_repair_mermaid(self, block: str) -> str:
        rules = self._load_mermaid_rules()
        prompt = (
            f"{rules}\n\n"
            "The following Mermaid code is broken or incompatible with Obsidian. Please fix it.\n"
            "Return ONLY the corrected code inside a mermaid block.\n\n"
            "BROKEN CODE:\n"
            f"```mermaid\n{block}\n```\n"
        )
        try:
            response = self.llm.answer_query(
                prompt,
                wiki_context="",
                custom_instruction="You are a Mermaid syntax expert for Obsidian.",
            )
        except Exception as e:
            logging.error(f"Mermaid LLM repair failed: {e}")
            return ""
        response = clean_llm_response(response or "")
        match = _MERMAID_BLOCK_RE.search(response)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _is_mermaid_broken(block: str) -> bool:
        """Heuristic: brackets unbalanced *outside* quoted labels and comments."""
        if not block.strip():
            return True
        scrubbed = _MERMAID_QUOTED_RE.sub("", block)
        scrubbed = _MERMAID_COMMENT_RE.sub("", scrubbed)
        if scrubbed.count("[") != scrubbed.count("]"):
            return True
        if scrubbed.count("(") != scrubbed.count(")"):
            return True
        if scrubbed.count("{") != scrubbed.count("}"):
            return True
        return False

    def _write_report(
        self, title: str, body: str, report_type: str, metadata: dict | None = None
    ) -> tuple[Path, str]:
        """Self-correct `body`, write a timestamped report file, return (path, full_markdown).

        The second element is the *complete* document — frontmatter + body —
        exactly as written to disk. Callers that need to mirror the file
        elsewhere (Insights/) should write those bytes verbatim to stay
        byte-identical with the canonical report.

        Callers that ignored the return value previously still work.
        """
        from core.version import VERSION

        body = self._self_correct(body)

        metadata = dict(metadata or {})
        metadata.update({
            "title": title,
            "type": report_type,
            "version": VERSION,
            "date_created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "input_chars": self.stats["input_chars"],
            "output_chars": len(body),
        })

        full_markdown = dump_markdown_with_metadata(metadata, body)
        safe_title = re.sub(r'[\\/*?:"<>|]', "-", title)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"✅{report_type}-{safe_title}-{timestamp}.md"
        output_path = FROM_LLM_DIR / filename
        output_path.write_text(full_markdown, encoding="utf-8")
        logging.info(f"Report generated: {output_path.name} ({len(body)} chars)")
        return output_path, full_markdown

    def execute(self, task_context: dict):
        raise NotImplementedError("Subclasses must implement execute()")
