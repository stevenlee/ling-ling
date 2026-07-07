#!/usr/bin/env python3
"""Re-sign historical insights with corrected quality signals.

Safe to run WHILE the daemon is up: this deliberately does NOT construct
RAGManager — ChromaDB is single-writer and the daemon owns it (opening the
same persistent DB from a second process has segfault-corrupted it before).
Instead it builds a minimal shim:

  - ``ef``: the same embedding function the daemon uses (pure HTTP to
    Ollama; embedding cache disabled so no sqlite is shared either)
  - ``get_all_indexed_titles``: filesystem stems from Cortex/ (pages/ and
    Notes/ are already covered inside compute_signals)

The frontmatter writes land in the vault, so the daemon's VaultWatcher will
pick them up and reindex through its own single-writer path — that is the
sanctioned reindex route.

Usage
-----
  python scripts/backfill_insight_signals.py --dry-run          # report only
  python scripts/backfill_insight_signals.py                    # unsigned only
  python scripts/backfill_insight_signals.py --force            # re-sign all, no LLM
  python scripts/backfill_insight_signals.py --force --refute   # + refute (1 LLM call/file)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import os  # noqa: E402

from core.config import (  # noqa: E402
    CORTEX_DIR,
    EMBEDDING_MAX_CHARS,
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    INSIGHT_SIGNALS_FILE,
)

# Not exported by core.config — rag_manager reads these from the env the same way.
OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
from maintenance.insight_signals_backfill import backfill_signals  # noqa: E402
from services.rag.embedding import build_embedding_function  # noqa: E402


class _NoChromaRAG:
    """compute_signals needs only .ef and .get_all_indexed_titles()."""

    def __init__(self):
        self.ef = build_embedding_function(
            provider=EMBEDDING_PROVIDER,
            model=EMBEDDING_MODEL,
            ollama_api_base=OLLAMA_API_BASE,
            gemini_api_key=GEMINI_API_KEY,
            max_chars=EMBEDDING_MAX_CHARS,
            cache_enabled=False,  # don't share the daemon's cache sqlite
            cache_db_path=None,
        )

    def get_all_indexed_titles(self) -> set:
        if CORTEX_DIR.exists():
            return {p.stem for p in CORTEX_DIR.rglob("*.md")}
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-sign already-signed insights")
    parser.add_argument("--refute", action="store_true", help="run the LLM refute pass")
    parser.add_argument("--dry-run", action="store_true", help="compute but do not write")
    args = parser.parse_args()

    if args.refute:
        print(
            "--refute needs an LLM client; run it via the daemon context instead.", file=sys.stderr
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # A force run is a full chronological replay: novelty must accumulate from
    # an empty history, so reset the sidecar (its old entries are stale-dim
    # and/or out-of-order anyway).
    if args.force and not args.dry_run and INSIGHT_SIGNALS_FILE.exists():
        INSIGHT_SIGNALS_FILE.unlink()
        print(f"reset novelty sidecar: {INSIGHT_SIGNALS_FILE}")

    result = backfill_signals(
        _NoChromaRAG(),
        llm=None,
        run_refute=False,
        force=args.force,
        dry_run=args.dry_run,
    )

    print(
        f"\nscanned={result.scanned} backfilled={result.backfilled} "
        f"resigned={result.resigned} skipped_signed={result.skipped_signed} "
        f"failed={len(result.failed)}{' (dry-run)' if args.dry_run else ''}"
    )
    if result.unresolved:
        print(f"\nunresolved source fragments ({len(result.unresolved)}):")
        for frag in result.unresolved:
            print(f"  - {frag}")
    for line in result.failed:
        print(f"FAILED: {line}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
