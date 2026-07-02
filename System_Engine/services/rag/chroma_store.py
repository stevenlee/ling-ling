"""ChromaDB store plumbing for RAG (P2e).

Everything Chroma-specific that isn't retrieval logic, moved from
services/rag_manager.py: the SQLite lock retry decorator, where-clause
construction (tag-boolean keys + section levels — the ONLY place Chroma's
`$and` syntax is assembled), collection creation, and the persisted
embedding-config validation.
"""

from __future__ import annotations

import logging
import time
from functools import wraps

from core.tag_manager import TagManager
from services.rag.embedding import get_effective_model_name


# Decorator to handle temporary database locks in SQLite
def retry_on_db_lock(retries=3, delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "database is locked" in str(e).lower() or "timeout" in str(e).lower():
                        logging.warning(f"Database locked, retrying {i + 1}/{retries}...")
                        time.sleep(delay * (i + 1))
                        last_err = e
                    else:
                        raise e
            raise last_err

        return wrapper

    return decorator


def sanitize_tag_key(tag_name: str) -> str:
    tag = TagManager.normalize(tag_name)
    if not tag:
        return ""
    sanitized = tag.replace("/", "_").replace("\\", "_")
    res = "".join(c for c in sanitized if c.isalnum() or c in ("_", "-"))
    while "__" in res:
        res = res.replace("__", "_")
    while "--" in res:
        res = res.replace("--", "-")
    res = res.strip("_").strip("-")
    return f"tag_{res}" if res else ""


def build_where_clause(
    tags: list[str] | None = None,
    section_path: list[str] | None = None,
    where_filter: dict | None = None,
) -> dict | None:
    filters: list[dict] = []

    if tags:
        for t in tags:
            san_tag = sanitize_tag_key(t)
            if san_tag:
                filters.append({san_tag: True})

    if section_path:
        for idx, level in enumerate(section_path):
            if idx < 6:
                filters.append({f"section_l{idx + 1}": level.lower().strip()})

    if where_filter:
        filters.append(where_filter)

    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"$and": filters}


def _mismatch_error(db_provider, db_model, db_dim, curr_provider, curr_model) -> str:
    return (
        f"Embedding configuration mismatch detected!\n"
        f"Database collection has: provider={db_provider}, model={db_model}, dimension={db_dim}\n"
        f"Current config expects: provider={curr_provider}, model={curr_model}\n"
        f"Please wipe the database and perform a full re-index to apply changes:\n"
        f"run 'python System_Engine/maintenance/init_rag.py --wipe'"
    )


def ensure_collection(client, ef, *, provider: str, model: str | None, skip_config_check: bool):
    """Get-or-create the wiki_pages collection, raising a clear config-mismatch
    error when the persisted embedding function conflicts with the current one."""
    try:
        if skip_config_check:
            try:
                return client.get_collection(name="wiki_pages", embedding_function=ef)
            except Exception:
                return client.create_collection(name="wiki_pages", embedding_function=ef)
        return client.get_or_create_collection(name="wiki_pages", embedding_function=ef)
    except ValueError as e:
        if "embedding function" in str(e).lower() or "conflict" in str(e).lower():
            curr_model = get_effective_model_name(provider, model)
            try:
                old_coll = client.get_collection(name="wiki_pages")
                db_metadata = old_coll.metadata
                db_provider: object = "unknown"
                db_model: object = "unknown"
                db_dim: object = "unknown"
                if not db_metadata or "embedding_provider" not in db_metadata:
                    db_provider = "local"
                    db_model = "all-MiniLM-L6-v2"
                    db_dim = 384
                else:
                    db_provider = db_metadata.get("embedding_provider")
                    db_model = db_metadata.get("embedding_model")
                    db_dim = int(db_metadata.get("embedding_dimension") or 0)
            except Exception:
                db_provider, db_model, db_dim = "unknown", "unknown", "unknown"

            error_msg = _mismatch_error(db_provider, db_model, db_dim, provider, curr_model)
            logging.critical(error_msg)
            raise ValueError(error_msg) from e
        raise e


def check_metadata_mismatch(collection, ef, *, provider: str, model: str | None) -> None:
    """Validate the persisted embedding config matches the current one.

    We avoid probing the embedding model on every startup — provider and
    model name (recorded in collection metadata) are authoritative. The
    dimension probe only runs when we genuinely need it: an empty
    collection that needs its metadata initialised. That makes startup
    a no-op for steady-state Gemini/Ollama setups (no wasted API call).
    """
    curr_model = get_effective_model_name(provider, model)
    db_metadata = collection.metadata or {}

    has_complete_meta = all(
        key in db_metadata
        for key in ("embedding_provider", "embedding_model", "embedding_dimension")
    )
    if (
        has_complete_meta
        and db_metadata.get("embedding_provider") == provider
        and db_metadata.get("embedding_model") == curr_model
    ):
        return

    if collection.count() == 0:
        try:
            curr_dim = len(ef(["test"])[0])
        except Exception as e:
            logging.warning(f"Could not automatically detect embedding dimension: {e}")
            curr_dim = 768 if provider in ("ollama", "gemini") else 384
        new_meta = {
            **db_metadata,
            "embedding_provider": provider,
            "embedding_model": curr_model,
            "embedding_dimension": curr_dim,
        }
        collection.modify(metadata=new_meta)
        return

    db_provider: str | None
    db_model: str | None
    db_dim: int
    if not db_metadata or "embedding_provider" not in db_metadata:
        db_provider = "local"
        db_model = "all-MiniLM-L6-v2"
        db_dim = 384
    else:
        db_provider = db_metadata.get("embedding_provider")
        db_model = db_metadata.get("embedding_model")
        db_dim = int(db_metadata.get("embedding_dimension") or 0)

    if db_provider == provider and db_model == curr_model:
        return

    error_msg = _mismatch_error(db_provider, db_model, db_dim, provider, curr_model)
    logging.critical(error_msg)
    raise ValueError(error_msg)
