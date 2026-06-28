import os
import sys
import logging
from pathlib import Path
import yaml

# Add System_Engine to sys.path
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from core.config import PAGES_DIR, NOTES_DIR, CORTEX_DIR, TAG_MAP_FILE
from core.tag_manager import TagManager
from core.vault_utils import _FRONTMATTER_NL_RE

logging.basicConfig(level=logging.INFO)

def clean_vault_tags():
    tm = TagManager(TAG_MAP_FILE)
    directories = [PAGES_DIR, NOTES_DIR, CORTEX_DIR]
    
    useless_tags = {"synthesis", "completed", "stitched", "longform"}
    
    count_modified = 0
    
    for directory in directories:
        if not directory.exists():
            continue
        for filepath in directory.rglob("*.md"):
            if filepath.name.startswith(".") or filepath.name.startswith("_"):
                continue
                
            try:
                content = filepath.read_text(encoding="utf-8")
            except Exception:
                continue
                
            fm = _FRONTMATTER_NL_RE.search(content)
            if not fm:
                continue
                
            try:
                data = yaml.safe_load(fm.group(1))
            except Exception:
                continue
                
            if not isinstance(data, dict):
                continue
                
            original_tags = data.get("tags", [])
            if isinstance(original_tags, str):
                original_tags = [original_tags]
            elif not isinstance(original_tags, list):
                original_tags = []
                
            if not original_tags:
                continue
                
            new_tags = set()
            aliases_to_add = set()
            modified = False
            
            # 1. Remove useless meta tags
            for tag in list(original_tags):
                if tm.normalize(tag) in useless_tags:
                    modified = True
                    if tm.normalize(tag) == "synthesis" and "type" not in data:
                        data["type"] = "synthesis"
                    elif tm.normalize(tag) == "stitched" and "type" not in data:
                        data["type"] = "stitched_article"
                    elif tm.normalize(tag) == "completed" and "status" not in data:
                        data["status"] = "completed"
                else:
                    # 2. Check bilingual translation
                    if tm.is_bilingual_needed(tag):
                        eq = tm.get_equivalent(tag)
                        if eq:
                            new_tags.add(tm.normalize(eq))
                            aliases_to_add.add(tag)
                            modified = True
                        else:
                            new_tags.add(tm.normalize(tag))
                    else:
                        new_tags.add(tm.normalize(tag))
                        
            final_tags = sorted(list(new_tags))
            
            if not modified and final_tags == original_tags:
                continue
                
            data["tags"] = final_tags
            
            if aliases_to_add:
                existing_aliases = data.get("aliases", [])
                if isinstance(existing_aliases, str):
                    existing_aliases = [existing_aliases]
                elif not isinstance(existing_aliases, list):
                    existing_aliases = []
                
                for a in aliases_to_add:
                    if a not in existing_aliases:
                        existing_aliases.append(a)
                data["aliases"] = existing_aliases
                
            new_fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
            body = content[fm.end():]
            filepath.write_text(f"---\n{new_fm}\n---\n{body}", encoding="utf-8")
            count_modified += 1
            logging.info(f"Cleaned {filepath.name}: aliases added {list(aliases_to_add)}, tags {original_tags} -> {final_tags}")
            
    logging.info(f"Done! Modified {count_modified} files.")

if __name__ == "__main__":
    clean_vault_tags()
