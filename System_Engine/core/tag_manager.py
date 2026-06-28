import logging
import re
import threading
from pathlib import Path

import yaml


_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_CJK_RE = re.compile(r'[一-鿿぀-ヿ가-힯]')
_SPACE_OR_UNDERSCORE_RE = re.compile(r'[\s_]+')
_MULTI_DASH_RE = re.compile(r'-+')

_TAG_FILE_BODY = (
    "# 標籤對照定義 (Tag Map)\n"
    "系統會自動從此處讀取標籤對照關係。"
    "您可以直接在 Obsidian 的「屬性 (Properties)」介面中新增或修改對照。\n"
)


class TagManager:
    def __init__(self, mapping_file: Path):
        self.mapping_file = mapping_file
        self.lock = threading.Lock()
        self._map: dict[str, str] = {}
        self.load()

    def load(self):
        if not self.mapping_file.exists():
            self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            self.save()
            return

        try:
            content = self.mapping_file.read_text(encoding="utf-8")
        except Exception as e:
            logging.error(f"TagManager: failed to read {self.mapping_file.name}: {e}")
            return

        try:
            match = _FRONTMATTER_RE.search(content)
            data = yaml.safe_load(match.group(1)) if match else yaml.safe_load(content)
            if isinstance(data, dict):
                self._map = {}
                for k, v in data.items():
                    norm_k = self.normalize(str(k))
                    if not norm_k:
                        continue
                    if isinstance(v, list):
                        if v:
                            self._map[norm_k] = str(v[0])
                    elif v is not None:
                        self._map[norm_k] = str(v)
        except Exception as e:
            logging.error(f"TagManager: failed to parse map from {self.mapping_file.name}: {e}")

    def save(self):
        try:
            sorted_map = {k: self._map[k] for k in sorted(self._map)}
            yaml_str = yaml.safe_dump(sorted_map, allow_unicode=True, sort_keys=False).strip()
            md_content = f"---\n{yaml_str}\n---\n{_TAG_FILE_BODY}"
            self.mapping_file.write_text(md_content, encoding="utf-8")
        except Exception as e:
            logging.error(f"TagManager: failed to save map: {e}")

    @staticmethod
    def normalize(tag: str) -> str:
        """Lowercase kebab-case form, stripped of surrounding hashes/dashes."""
        if not tag:
            return ""
        t = tag.lower().strip().lstrip("#")
        t = _SPACE_OR_UNDERSCORE_RE.sub("-", t)
        t = _MULTI_DASH_RE.sub("-", t)
        return t.strip("-")

    @staticmethod
    def normalize_list(tags) -> list[str]:
        """Normalize, dedupe, and sort a tag list. Drops empties."""
        if not tags:
            return []
        return sorted({n for n in (TagManager.normalize(t) for t in tags) if n})

    def get_equivalent(self, tag: str) -> str | None:
        return self._map.get(self.normalize(tag))

    def add_mapping(self, source: str, target: str):
        s_norm = self.normalize(source)
        t_norm = self.normalize(target)
        if not s_norm or not t_norm:
            return

        with self.lock:
            # Re-read to capture any manual edits the user made at runtime.
            self.load()
            if s_norm not in self._map:
                self._map[s_norm] = t_norm
                self.save()
                logging.info(f"TagManager: learned mapping: {s_norm} -> {t_norm}")

    def get_all_tags(self) -> set[str]:
        tags = set()
        for k, v in self._map.items():
            tags.add(k)
            tags.add(v)
        return tags

    @staticmethod
    def is_bilingual_needed(tag: str) -> bool:
        """True if the tag contains CJK characters and likely needs translation."""
        return bool(_CJK_RE.search(tag))

    @staticmethod
    def move_cjk_to_aliases(tags: list[str], current_aliases: list[str]) -> tuple[list[str], list[str]]:
        """Filters CJK tags out of the tags list and appends them to current_aliases."""
        if not tags:
            return [], current_aliases or []
            
        new_tags = []
        aliases_set = set(current_aliases or [])
        
        for t in tags:
            if TagManager.is_bilingual_needed(t):
                aliases_set.add(t)
            else:
                new_tags.append(t)
                
        return new_tags, sorted(list(aliases_set))
