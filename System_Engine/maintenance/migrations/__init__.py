"""Database migrations applied against the ChromaDB knowledge base.

Each migration is a module in this package that defines:

    MIGRATION_ID: str       # filename stem; sortable id, e.g. "001_normalize_chroma_tags"
    DESCRIPTION:  str       # one-line summary shown in `--list`
    def run(rag_manager) -> dict  # idempotent; returns a stats dict for the log

Conventions:
  - Filenames start with a zero-padded numeric prefix so ordering is by name.
  - `run` must be safe to call on an already-migrated DB (e.g. detect & skip).
  - Returned stats are stored alongside the migration record for later audit.
"""
