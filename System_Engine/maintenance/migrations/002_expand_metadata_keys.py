"""Expand tag strings to boolean metadata properties and section_paths to hierarchical level keys.

This migration discovers all existing chunks, parses their tag strings and section path
strings, and expands them to tag_<sanitized_tag>: True and section_l1...section_l6 properties.
It also resolves stable doc_ids for existing chunks by scanning the vault for matching file names.

Idempotent: safe to run repeatedly.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from core.config import PAGES_DIR, NOTES_DIR, WIKI_VAULT_DIR
from core.tag_manager import TagManager


MIGRATION_ID = "002_expand_metadata_keys"
DESCRIPTION = "Expand tags to boolean fields and section_path to level properties in chunk metadata."


def _parse_tags(tags_field: str) -> list[str]:
    if not tags_field:
        return []
    return [t.strip() for t in tags_field.strip(",").split(",") if t.strip()]


def _parse_section_path(section_marker: str) -> list[str]:
    if not section_marker:
        return []
    if isinstance(section_marker, str) and section_marker.startswith(">"):
        return [s.strip() for s in section_marker.split(">") if s.strip()]
    return []


def run(rag_manager) -> dict:
    coll = rag_manager.collection
    results = coll.get(include=["metadatas"])
    ids = results.get("ids", []) or []
    metadatas = results.get("metadatas", []) or []

    update_ids: list[str] = []
    update_metas: list[dict] = []
    
    # Pre-cache vault file paths for quick doc_id resolution
    file_map: dict[str, Path] = {}
    for directory in (PAGES_DIR, NOTES_DIR):
        if directory.exists():
            for p in directory.rglob("*.md"):
                file_map[p.name] = p

    for cid, meta in zip(ids, metadatas):
        meta = meta or {}
        changed = False
        new_meta = dict(meta)

        # 1. Expand tags to boolean flags
        raw_tags = meta.get("tags", "") or ""
        norm_tags = TagManager.normalize_list(_parse_tags(raw_tags))
        
        # Ensure tag_<name>: True is written for each tag
        for tag in norm_tags:
            san_tag = rag_manager._sanitize_tag_key(tag)
            if san_tag and san_tag not in new_meta:
                new_meta[san_tag] = True
                changed = True

        # Keep tags_display format
        tags_display = f",{','.join(norm_tags)}," if norm_tags else ""
        if new_meta.get("tags") != tags_display:
            new_meta["tags"] = tags_display
            changed = True

        # 2. Expand section_path to hierarchical levels (l1 to l6)
        raw_section = meta.get("section_path", "") or ""
        if isinstance(raw_section, str) and raw_section.startswith(">"):
            levels = _parse_section_path(raw_section)
            new_meta["section_depth"] = len(levels)
            for idx in range(6):
                key = f"section_l{idx + 1}"
                if idx < len(levels):
                    val = levels[idx].lower().strip()
                else:
                    val = ""
                if new_meta.get(key) != val:
                    new_meta[key] = val
                    changed = True
            
            full_path = " > ".join(s.lower().strip() for s in levels)
            if new_meta.get("section_path_full") != full_path:
                new_meta["section_path_full"] = full_path
                changed = True

        # 3. Resolve stable doc_id and source_path
        if "doc_id" not in new_meta:
            source_name = meta.get("source")
            doc_id = None
            source_path = ""
            
            if source_name and source_name in file_map:
                filepath = file_map[source_name]
                doc_id = rag_manager._get_doc_id(filepath)
                try:
                    rel_path = filepath.resolve().relative_to(WIKI_VAULT_DIR.resolve())
                except Exception:
                    try:
                        rel_path = filepath.relative_to(WIKI_VAULT_DIR)
                    except Exception:
                        rel_path = filepath
                source_path = str(rel_path).replace("\\", "/")
            elif source_name:
                # Fallback if file is no longer on disk
                doc_id = hashlib.sha256(source_name.encode("utf-8")).hexdigest()
                source_path = source_name
            
            if doc_id:
                new_meta["doc_id"] = doc_id
                new_meta["source_path"] = source_path
                changed = True

        if changed:
            update_ids.append(cid)
            update_metas.append(new_meta)

    if update_ids:
        # ChromaDB caps update batch size; chunk to be safe on large vaults.
        BATCH = 500
        for i in range(0, len(update_ids), BATCH):
            coll.update(ids=update_ids[i:i + BATCH], metadatas=update_metas[i:i + BATCH])
        logging.info(f"{MIGRATION_ID}: upgraded metadata for {len(update_ids)} chunks")

    return {
        "chunks_scanned": len(ids),
        "chunks_upgraded": len(update_ids),
    }
