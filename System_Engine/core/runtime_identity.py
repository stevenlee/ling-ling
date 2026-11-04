"""Process-local checkout provenance; never relabel a live process after a pull."""

from __future__ import annotations

import copy
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime

from core import config
from core.version import BUILD_DATE

_REPO_ROOT = config.SYSTEM_ENGINE_DIR.parent


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    return result.stdout.strip()


def _capture_identity() -> dict:
    commit, dirty = None, None
    try:
        head = _git("rev-parse", "HEAD")
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            commit = head
            dirty = bool(
                _git(
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                    "--",
                    "System_Engine",
                    "pyproject.toml",
                    "Makefile",
                )
            )
            # A checkout may move while the snapshot is being collected.
            if _git("rev-parse", "HEAD") != head:
                commit, dirty = None, None
    except (OSError, subprocess.SubprocessError):
        # Unknown dirtiness must never masquerade as a reproducible clean build.
        dirty = None
    return {
        "schema_version": 1,
        "session_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit,
        "engine_dirty": dirty,
        "build_date": BUILD_DATE,
    }


# Deliberately frozen at first import, before daemon work starts. This is a
# checkout snapshot, not proof of every late-imported module's loaded bytes.
_IDENTITY = _capture_identity()


def runtime_identity() -> dict:
    return copy.deepcopy(_IDENTITY)


def runtime_config() -> dict:
    """Allowlisted diagnostics only; never serialize env, credentials or URLs."""
    return {
        "embedding_provider": config.EMBEDDING_PROVIDER,
        "embedding_model": config.EMBEDDING_MODEL,
        "reranker_enabled": config.RERANKER_ENABLED,
        "reranker_model": config.RERANKER_MODEL,
        "facet_index_enabled": config.FACET_INDEX_ENABLED,
        "cross_lingual_enabled": config.CROSS_LINGUAL_ENABLED,
        "evidence_traceback_enabled": config.settings.EVIDENCE_TRACEBACK_ENABLED,
        "evidence_traceback_apply": config.settings.EVIDENCE_TRACEBACK_APPLY,
        "evidence_traceback_max_distance": config.settings.EVIDENCE_TRACEBACK_MAX_DISTANCE,
    }
