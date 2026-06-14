"""Engine build date — the commit date of the running code.

Replaces a hand-maintained semver (`VERSION = "0.3.0"`) that nobody bumped,
so it lied: reports kept claiming 0.3.0 while the pipeline changed under it.
The git commit date is recorded automatically and never goes stale. Falls
back to this file's mtime when git is unavailable (e.g. a packaged build with
no `.git`), so import never fails and never blocks.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

# System_Engine/core/version.py → repo root is three levels up.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _compute_build_date() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "log", "-1", "--format=%cd", "--date=short"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        date = proc.stdout.strip()
        if proc.returncode == 0 and date:
            return date
    except Exception:
        pass
    # No git (packaged build, shallow copy, …): use this file's mtime.
    try:
        return datetime.fromtimestamp(Path(__file__).stat().st_mtime).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


# Computed once at import; module caching means the git call runs at most once
# per process.
BUILD_DATE = _compute_build_date()
