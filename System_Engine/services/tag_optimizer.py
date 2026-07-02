import logging
from pathlib import Path

from services.llm_client import LLMClient
from core.tag_manager import TagManager
from core.config import TAG_MAP_FILE
from core.vault_utils import update_file_tags
from core.parser import parse_markdown_metadata


class TagOptimizer:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.tag_manager = TagManager(TAG_MAP_FILE)

    def _find_best_matches(self, keywords: list[str]) -> list[str]:
        """Find best matches from the tag dictionary or return the original keyword."""

        final_tags = []
        for kw in keywords:
            # Check if this keyword is a known source (e.g., Chinese term)
            mapped = self.tag_manager.get_equivalent(kw)
            if mapped:
                final_tags.append(mapped)
                # Keep original keyword as an alias by appending it to final tags
                # the `move_cjk_to_aliases` will catch it if it's CJK
                final_tags.append(kw)
            else:
                final_tags.append(TagManager.normalize(kw))

                # Auto-add to dictionary if it's not CJK (so it becomes a standard English tag)
                if not TagManager.is_bilingual_needed(kw):
                    self.tag_manager.add_mapping(kw, kw)

        return TagManager.normalize_list(final_tags)

    def generate_and_optimize(self, filepath: Path) -> bool:
        """Reads a file, extracts existing tags, auto-generates up to 5, and rewrites frontmatter."""
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception as e:
            logging.error(f"TagOptimizer: Failed to read {filepath}: {e}")
            return False

        # Parse existing tags and aliases
        frontmatter = parse_markdown_metadata(content) or {}
        existing_tags = frontmatter.get("tags", [])
        if isinstance(existing_tags, str):
            existing_tags = [existing_tags]

        existing_aliases = frontmatter.get("aliases", [])
        if isinstance(existing_aliases, str):
            existing_aliases = [existing_aliases]

        # LLM instruction to generate keywords
        prompt = (
            "Analyze the following document and provide up to 5 highly relevant keywords (tags). "
            "Prefer standard English technical terms for concepts. You may use Chinese terms if it's highly specific. "
            f"If there is a known tag in the existing dictionary that matches perfectly, use it. "
            f"Existing dictionary sample: {list(self.tag_manager.get_all_tags())[:30]}\n\n"
            f"Document Content:\n{content[:4000]}"
        )

        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of up to 5 relevant tags",
                }
            },
            "required": ["tags"],
        }

        logging.info(f"TagOptimizer: Generating tags for {filepath.name}...")
        try:
            res = self.llm.generate_structured(prompt, schema)
            generated_tags = res.get("tags", [])
        except Exception as e:
            logging.error(f"TagOptimizer: LLM generation failed for {filepath}: {e}")
            generated_tags = []

        # Combine existing and generated tags
        combined_raw_tags = existing_tags + generated_tags

        # Map through dictionary
        mapped_tags = self._find_best_matches(combined_raw_tags)

        # Move CJK tags to aliases
        final_tags, final_aliases = TagManager.move_cjk_to_aliases(mapped_tags, existing_aliases)

        # Check if the combined set of aliases and tags has actually changed to avoid unnecessary writes
        old_tags_set = set(TagManager.normalize_list(existing_tags))
        old_aliases_set = set(existing_aliases)
        new_tags_set = set(final_tags)
        new_aliases_set = set(final_aliases)

        if old_tags_set == new_tags_set and old_aliases_set == new_aliases_set:
            logging.info(f"TagOptimizer: No changes needed for {filepath.name}")
            return True

        # Rewrite file
        try:
            update_file_tags(filepath, final_tags, add_aliases=final_aliases)
            logging.info(f"TagOptimizer: Successfully optimized tags for {filepath.name}")
            return True
        except Exception as e:
            logging.error(f"TagOptimizer: Failed to rewrite file {filepath}: {e}")
            return False
