import logging
import yaml
import re
from pathlib import Path
from datetime import datetime
from core.config import INDEX_FILE, PAGES_DIR, NOTES_DIR, WIKI_VAULT_DIR, RAW_CONSOLIDATE_DIR

def update_wiki_index(filepath: Path = None, title: str = None):
    """
    Regenerates the entire index.md based on a full scan of Notes/ and pages/.
    Groups files by folder, sorts alphabetically, and extracts YAML metadata.
    """
    def natural_sort_key(s):
        s = str(s or "")
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    try:
        logging.info("Wiki Utils: Regenerating Knowledge Map Index...")
        
        sections = {
            "Notes": {"dir": NOTES_DIR, "icon": "✍️", "files": {}},
            "Entities": {"dir": PAGES_DIR, "icon": "🤖", "files": {}}
        }

        def get_metadata(f_path):
            try:
                content = f_path.read_text(encoding='utf-8')
                fm_match = re.search(r'^---\s*\n(.*?)\n---\s*', content, re.DOTALL)
                # Default date from file modification time
                mtime = datetime.fromtimestamp(f_path.stat().st_mtime).strftime("%Y-%m-%d")
                meta = {"title": f_path.stem, "tags": [], "date": mtime}
                
                if fm_match:
                    data = yaml.safe_load(fm_match.group(1))
                    if isinstance(data, dict):
                        meta["title"] = str(data.get("title") or f_path.stem)
                        meta["tags"] = data.get("tags", [])
                        if not isinstance(meta["tags"], list): meta["tags"] = [meta["tags"]]
                        # Override mtime only if YAML has a valid date
                        yaml_date = str(data.get("date_created") or data.get("created") or data.get("date") or "")
                        if yaml_date:
                            meta["date"] = yaml_date
                return meta
            except:
                return {"title": f_path.stem, "tags": [], "date": ""}

        # 1. Scan Directories
        for s_name, s_info in sections.items():
            base_dir = s_info["dir"]
            if not base_dir.exists(): continue
            
            for f in base_dir.rglob("*.md"):
                if f.name.startswith(".") or f.name.startswith("_"): continue
                
                rel_path = f.relative_to(base_dir)
                folder_name = "Root" if len(rel_path.parts) == 1 else rel_path.parts[0]
                
                if folder_name not in s_info["files"]:
                    s_info["files"][folder_name] = []
                
                s_info["files"][folder_name].append(get_metadata(f))

        # 2. Specially scan RAW_CONSOLIDATE_DIR and inject into Entities
        if RAW_CONSOLIDATE_DIR.exists():
            for f in RAW_CONSOLIDATE_DIR.glob("*.md"):
                if f.name.startswith(".") or f.name.startswith("_"): continue
                # Use the stem as the folder name to group with the processed entity folder
                folder_name = f.stem
                if folder_name not in sections["Entities"]["files"]:
                    sections["Entities"]["files"][folder_name] = []
                
                meta = get_metadata(f)
                sections["Entities"]["files"][folder_name].append(meta)

        # 2. Build Markdown
        from core.version import VERSION
        lines = [f"# 🎀 Knowledge Dashboard (v{VERSION})", "---", ""]
        
        for s_name, s_info in sections.items():
            lines.append(f"## {s_info['icon']} {s_name}")
            
            # Sort folders alphabetically
            sorted_folders = sorted(s_info["files"].keys())
            
            for folder in sorted_folders:
                # Use natural sort key for titles
                files = sorted(s_info["files"][folder], key=lambda x: natural_sort_key(x["title"]))
                
                if folder == "Root":
                    for meta in files:
                        tag_str = f" `{'` `'.join(meta['tags'][:3])}`" if meta["tags"] else ""
                        date_str = f" | 📅 {meta['date']}" if meta['date'] else ""
                        lines.append(f"- [[{meta['title']}]] {tag_str}{date_str}")
                else:
                    # Collapsible Callout for subdirectories
                    folder_date = files[0]["date"] if files and files[0]["date"] else ""
                    date_suffix = f" | 📅 {folder_date}" if folder_date else ""
                    lines.append(f"> [!abstract]- 📂 {folder} ({len(files)} items){date_suffix}")
                    for meta in files:
                        tag_str = f" `{'` `'.join(meta['tags'][:3])}`" if meta["tags"] else ""
                        lines.append(f"> - [[{meta['title']}]] {tag_str}")
                lines.append("")
            lines.append("")

        INDEX_FILE.write_text("\n".join(lines), 'utf-8')
        logging.info("Wiki Utils: index.md has been successfully updated.")
        
    except Exception as e:
        logging.error(f"Wiki Utils: Failed to regenerate index.md: {e}")

def update_file_tags(filepath: Path, tags: list[str]):
    """
    Updates the YAML frontmatter of the markdown file with new tags.
    """
    import yaml
    import re
    content = filepath.read_text(encoding='utf-8')
    
    # Regex to match frontmatter, without enforcing trailing newline
    fm_match = re.search(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', content, re.DOTALL)
    if not fm_match:
        # If no frontmatter, create it
        tag_str = ", ".join(tags)
        new_content = f"---\ntags: [{tag_str}]\n---\n\n" + content
        filepath.write_text(new_content, encoding='utf-8')
        return

    try:
        frontmatter = fm_match.group(1)
        body = content[fm_match.end():]
        data = yaml.safe_load(frontmatter)
        if not isinstance(data, dict): data = {}
        
        data['tags'] = tags
        
        # Dump back to YAML
        new_fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
        new_content = f"---\n{new_fm}\n---\n{body}"
        filepath.write_text(new_content, encoding='utf-8')
    except Exception as e:
        raise ValueError(f"YAML update failed: {e}")

def find_note(title: str) -> Path | None:
    """Finds a note by its title (stem) in Pages/ or Notes/."""
    for directory in [PAGES_DIR, NOTES_DIR]:
        if not directory.exists(): continue
        p = next(directory.rglob(f"{title}.md"), None)
        if p: return p
    return None

def get_note_content(title_or_path: str | Path) -> str:
    """Retrieves the content of a note by title or Path."""
    if isinstance(title_or_path, str):
        path = find_note(title_or_path)
    else:
        path = title_or_path
        
    if path and path.exists():
        return path.read_text(encoding='utf-8')
    return ""

def update_note_with_meta(filepath: Path, body: str, meta: dict):
    """Writes a note with provided body and metadata (merging with existing if needed)."""
    from core.parser import dump_markdown_with_metadata, parse_markdown_metadata
    
    existing_meta = {}
    if filepath.exists():
        existing_meta = parse_markdown_metadata(filepath.read_text(encoding='utf-8'))
        
    # Merge: meta overrides existing_meta
    existing_meta.update(meta)
    
    new_content = dump_markdown_with_metadata(existing_meta, body)
    filepath.write_text(new_content, encoding='utf-8')
