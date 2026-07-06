"""pack_code — bundle repo source files into a vault CodeReview/ note.

The coder agents (@ling-code-review / @ling-architect) are vault-only: they never
read outside lings-desktop/. This CLI is the bridge — the USER runs it to copy
chosen source files INTO the vault as a `packed-code` note (fenced code + an
ast-harvested identifier manifest). It is deterministic, makes zero LLM calls,
and NEVER executes the code it reads.

    python System_Engine/tools/pack_code.py <SRC>... [--title NAME]

SRC is a file or directory, relative to the repo root (or absolute inside it).
Directories recurse for *.py. Output: lings-desktop/CodeReview/<title>.md
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # System_Engine

from core.config import CODE_REVIEW_DIR, PROJECT_ROOT  # noqa: E402
from core.vault_utils import sanitize_filename  # noqa: E402

_MAX_FILE_BYTES = 200 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024
_FENCE_LANG = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".md": "markdown"}


def _resolve_inside_repo(raw: str) -> Path:
    """Resolve `raw` against the repo root and refuse anything outside it
    (blocks `..` and symlink escapes — we only ever pack our own tree)."""
    p = Path(raw)
    p = p if p.is_absolute() else (PROJECT_ROOT / p)
    p = p.resolve()
    if not p.is_relative_to(PROJECT_ROOT.resolve()):
        raise SystemExit(f"✗ refusing to pack a path outside the repo: {raw}")
    return p


def _collect(srcs: list[str]) -> list[Path]:
    files: list[Path] = []
    repo = PROJECT_ROOT.resolve()
    for raw in srcs:
        p = _resolve_inside_repo(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if not f.is_file():
                    continue
                # A symlinked file inside the tree can point outside the repo —
                # the SRC-level check above doesn't cover it. Never silent-skip.
                if not f.resolve().is_relative_to(repo):
                    print(f"⚠ skipped (resolves outside repo): {f}")
                    continue
                files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            raise SystemExit(f"✗ not found: {raw}")
    # de-dup, preserve order
    seen: set[Path] = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _harvest_identifiers(path: Path, text: str) -> list[str]:
    """Top-level + nested function/class names, via ast (Python only)."""
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in names:
                names.append(node.name)
    return names


def _git_commit() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def pack(srcs: list[str], title: str | None) -> Path:
    files = _collect(srcs)
    if not files:
        raise SystemExit("✗ nothing to pack")

    total = 0
    sections: list[str] = []
    identifiers: list[str] = []
    rels: list[str] = []
    total_lines = 0
    for f in files:
        size = f.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise SystemExit(f"✗ {f} is {size} bytes (> {_MAX_FILE_BYTES}); refusing")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise SystemExit(f"✗ total exceeds {_MAX_TOTAL_BYTES} bytes; pack fewer files")
        text = f.read_text(encoding="utf-8")
        rel = str(f.relative_to(PROJECT_ROOT.resolve()))
        rels.append(rel)
        total_lines += text.count("\n") + 1
        for name in _harvest_identifiers(f, text):
            if name not in identifiers:
                identifiers.append(name)
        lang = _FENCE_LANG.get(f.suffix, "")
        sections.append(f"## {rel}\n\n```{lang}\n{text}\n```")

    if not title:
        title = Path(rels[0]).stem if len(files) == 1 else "packed-code"
    safe = sanitize_filename(title)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids_yaml = "\n".join(f"  - {name}" for name in identifiers) or "  []"
    srcs_yaml = "\n".join(f"  - {r}" for r in rels)
    frontmatter = (
        "---\n"
        "type: packed-code\n"
        f"title: {safe}\n"
        f"packed_at: {stamp}\n"
        f"git_commit: {_git_commit()}\n"
        f"file_count: {len(files)}\n"
        f"total_lines: {total_lines}\n"
        "source_paths:\n"
        f"{srcs_yaml}\n"
        "identifiers:\n"
        f"{ids_yaml}\n"
        "---\n"
    )
    body = frontmatter + "\n# Packed Code: " + safe + "\n\n" + "\n\n".join(sections) + "\n"

    CODE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = CODE_REVIEW_DIR / f"{safe}.md"
    out.write_text(body, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Pack repo source into a vault CodeReview/ note.")
    ap.add_argument("src", nargs="+", help="file(s)/dir(s), relative to repo root")
    ap.add_argument("--title", default=None, help="note title (default: filename or 'packed-code')")
    args = ap.parse_args()
    out = pack(args.src, args.title)
    n = out.read_text(encoding="utf-8").count("\n## ")
    print(f"🎀 packed {n} file(s) → {out.relative_to(PROJECT_ROOT.resolve())}")
    print(f"   review it:  @ling-code-review [[{out.stem}]]")


if __name__ == "__main__":
    main()
