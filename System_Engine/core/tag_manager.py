import json
import logging
from pathlib import Path
import re
import threading

class TagManager:
    def __init__(self, mapping_file: Path):
        self.mapping_file = mapping_file
        self.lock = threading.Lock()
        self._map = {}
        self.load()

    def load(self):
        if not self.mapping_file.exists():
            self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            self.save()
            return
        
        try:
            import yaml
            content = self.mapping_file.read_text(encoding='utf-8')
            # Extract YAML from frontmatter
            match = re.search(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
            if match:
                data = yaml.safe_load(match.group(1))
                if data and isinstance(data, dict):
                    # In flat structure, everything in frontmatter is a mapping
                    self._map = data
            else:
                # Fallback
                try:
                    data = yaml.safe_load(content)
                    if data and isinstance(data, dict):
                        self._map = data
                except: pass
        except Exception as e:
            logging.error(f"TagManager: Failed to load map from {self.mapping_file.name}: {e}")

    def save(self):
        try:
            import yaml
            # Sort alphabetically by key for user-friendly manual editing
            sorted_map = {k: self._map[k] for k in sorted(self._map.keys())}
            
            yaml_str = yaml.safe_dump(sorted_map, allow_unicode=True, sort_keys=False).strip()
            
            md_content = f"---\n{yaml_str}\n---\n# 標籤對照定義 (Tag Map)\n系統會自動從此處讀取標籤對照關係。您可以直接在 Obsidian 的「屬性 (Properties)」介面中新增或修改對照。\n"
            
            with open(self.mapping_file, 'w', encoding='utf-8') as f:
                f.write(md_content)
        except Exception as e:
            logging.error(f"TagManager: Failed to save map: {e}")

    def normalize(self, tag: str) -> str:
        """Standardizes tag to lowercase kebab-case."""
        if not tag: return ""
        # 1. Lowercase and remove surrounding punctuation/hashes
        t = tag.lower().strip().lstrip('#')
        # 2. Replace spaces and underscores with dashes
        t = re.sub(r'[\s_]+', '-', t)
        # 3. Remove consecutive dashes
        t = re.sub(r'-+', '-', t)
        # 4. Strip dashes from ends
        t = t.strip('-')
        return t

    def get_equivalent(self, tag: str) -> str:
        """Returns the mapped English equivalent if it exists in the local map."""
        norm_tag = self.normalize(tag)
        return self._map.get(norm_tag)

    def add_mapping(self, source: str, target: str):
        """Adds a new learning mapping to the table."""
        s_norm = self.normalize(source)
        t_norm = self.normalize(target)
        if not s_norm or not t_norm: return
        
        with self.lock:
            # Reload to capture any manual edits by the user during runtime
            self.load()
            if s_norm not in self._map:
                self._map[s_norm] = t_norm
                self.save()
                logging.info(f"TagManager: Learned mapping: {s_norm} -> {t_norm}")

    def is_bilingual_needed(self, tag: str) -> bool:
        """Returns True if the tag contains non-English characters (suggesting it needs a translation)."""
        # Range covers CJK characters
        return bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', tag))
