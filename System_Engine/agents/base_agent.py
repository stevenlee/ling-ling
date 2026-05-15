import logging
import re
from pathlib import Path
from datetime import datetime
from core.config import PROMPTS_DIR, FROM_LLM_DIR, settings
from core.parser import clean_llm_response, parse_markdown_metadata, dump_markdown_with_metadata

class BaseAgent:
    def __init__(self, llm, rag=None):
        self.llm = llm
        self.rag = rag
        self.stats = {
            "input_chars": 0,
            "output_chars": 0
        }

    def _load_prompt(self, prompt_name: str) -> str:
        """Loads a prompt template from the Prompts directory."""
        if not prompt_name.endswith(".md"):
            prompt_name += ".md"
        prompt_path = PROMPTS_DIR / prompt_name
        if prompt_path.exists():
            content = prompt_path.read_text(encoding='utf-8')
            # Track input chars (approximate)
            self.stats["input_chars"] += len(content)
            return content
        logging.warning(f"Prompt template not found: {prompt_name}")
        return ""

    def _load_mermaid_rules(self) -> str:
        """Loads Obsidian-specific Mermaid rules."""
        return self._load_prompt("mermaid_rules.md")

    def _self_correct(self, content: str) -> str:
        """
        Performs invisible healing on LLM output.
        Currently handles:
        - Markdown unwrapping
        - Mermaid syntax validation and repair
        """
        # 1. Standard Markdown Unwrapping
        content = clean_llm_response(content)
        
        # 2. Mermaid Syntax Healing
        if "```mermaid" in content:
            content = self._fix_mermaid_syntax(content)
        elif any(kw in content for kw in ["graph TD", "graph LR", "flowchart TD", "flowchart LR", "sequenceDiagram", "gantt", "classDiagram"]):
            # Heuristic: if mermaid keywords are found but no backticks, wrap the likely block
            # This is a simple wrapper for the whole content if it looks like a single diagram
            if not content.strip().startswith("```"):
                logging.info("Detected raw Mermaid code without backticks. Wrapping automatically...")
                content = f"```mermaid\n{content.strip()}\n```"
            
        return content

    def _fix_mermaid_syntax(self, content: str) -> str:
        """
        Detects mermaid blocks and attempts to fix them using LLM if needed.
        """
        # This is a simplified version. A more robust one would use a parser or multiple passes.
        mermaid_blocks = re.findall(r'```mermaid\n(.*?)\n```', content, re.DOTALL)
        if not mermaid_blocks:
            return content
            
        fixed_content = content
        for block in mermaid_blocks:
            # Simple heuristic: if it looks broken (e.g., unbalanced brackets), try to fix it.
            if self._is_mermaid_broken(block):
                logging.info("Detected potentially broken Mermaid block. Attempting self-correction...")
                rules = self._load_mermaid_rules()
                repair_prompt = f"""
{rules}

The following Mermaid code is broken or incompatible with Obsidian. Please fix it.
Return ONLY the corrected code inside a mermaid block.

BROKEN CODE:
```mermaid
{block}
```
"""
                correction = self.llm.answer_query(repair_prompt, wiki_context="", custom_instruction="You are a Mermaid syntax expert for Obsidian.")
                correction = clean_llm_response(correction)
                # Extract the code from the response
                new_block_match = re.search(r'```mermaid\n(.*?)\n```', correction, re.DOTALL)
                if new_block_match:
                    new_block = new_block_match.group(1).strip()
                    fixed_content = fixed_content.replace(block, new_block)
                    
        return fixed_content

    def _is_mermaid_broken(self, block: str) -> bool:
        """Simple heuristic for broken mermaid syntax."""
        # Unbalanced brackets
        if block.count('[') != block.count(']') or block.count('(') != block.count(')'):
            return True
        # Common illegal characters in node IDs if not quoted
        # (This is just a placeholder for more complex logic)
        return False

    def _write_report(self, title: str, body: str, report_type: str, metadata: dict = None) -> Path:
        """Standardized report writing with stats and metadata."""
        if metadata is None:
            metadata = {}
            
        from core.version import VERSION
        metadata.update({
            "title": title,
            "type": report_type,
            "version": VERSION,
            "date_created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "input_chars": self.stats["input_chars"],
            "output_chars": len(body)
        })
        
        # Ensure we don't have redundant wrappers
        body = self._self_correct(body)
        
        full_markdown = dump_markdown_with_metadata(metadata, body)
        
        # Create a safe filename
        safe_title = re.sub(r'[\\/*?:"<>|]', "-", title)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"✅{report_type}-{safe_title}-{timestamp}.md"
        
        output_path = FROM_LLM_DIR / filename
        output_path.write_text(full_markdown, encoding='utf-8')
        logging.info(f"Report generated: {output_path.name} ({len(body)} chars)")
        return output_path

    def execute(self, task_context: dict):
        """Abstract method for agent execution."""
        raise NotImplementedError("Subclasses must implement execute()")
