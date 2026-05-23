"""Normalize tag strings in every ChromaDB chunk's metadata.

Historic chunks were written with whatever case/format the LLM produced
(`#AI` vs `#ai` vs `#AI Agent` etc.), so the same logical tag could appear
under multiple keys in tag-cluster sampling. After this migration every
chunk's `tags` metadata field uses `TagManager.normalize` form.

Idempotent: re-running on an already-normalized DB is a no-op.
"""

from __future__ import annotations

import logging

from core.tag_manager import TagManager


MIGRATION_ID = "001_normalize_chroma_tags"
DESCRIPTION = "Normalize all tag strings in ChromaDB chunk metadata (lower-case + kebab)."


def _parse(tags_field: str) -> list[str]:
    if not tags_field:
        return []
    return [t.strip() for t in tags_field.strip(",").split(",") if t.strip()]


def _encode(tags: list[str]) -> str:
    return f",{','.join(tags)}," if tags else ""


def run(rag_manager) -> dict:
    coll = rag_manager.collection
    results = coll.get(include=["metadatas"])
    ids = results.get("ids", []) or []
    metadatas = results.get("metadatas", []) or []

    update_ids: list[str] = []
    update_metas: list[dict] = []
    for cid, meta in zip(ids, metadatas):
        meta = meta or {}
        raw = meta.get("tags", "") or ""
        norm = TagManager.normalize_list(_parse(raw))
        new_field = _encode(norm)
        if new_field != raw:
            new_meta = dict(meta)
            new_meta["tags"] = new_field
            update_ids.append(cid)
            update_metas.append(new_meta)

    if update_ids:
        # ChromaDB caps update batch size; chunk to be safe on large vaults.
        BATCH = 500
        for i in range(0, len(update_ids), BATCH):
            coll.update(ids=update_ids[i:i + BATCH], metadatas=update_metas[i:i + BATCH])
        logging.info(f"{MIGRATION_ID}: updated {len(update_ids)} chunks")

    return {
        "chunks_scanned": len(ids),
        "chunks_updated": len(update_ids),
    }
