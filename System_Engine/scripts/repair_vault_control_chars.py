"""One-shot stock repair: JSON-escape control chars in vault pages.

LLM digests once carried unescaped LaTeX inside JSON strings; json.loads
silently decoded `\\forall` → FF+orall, `\\binom` → BS+inom, `\\ell` →
ESC+ll, `\\rho` → CR+ho. The root cause is fixed (core/json_extract
repair pass + digest_value_to_text healing), but pages written before
the fix still carry the corruption in two places:

1. The markdown body (Part Digest Appendix) holds the control chars RAW.
2. The `part_digest:` frontmatter block holds them YAML-ESCAPED (`\\f`,
   `\\b`, `\\r`) — invisible to a raw scan, but yaml.safe_load hands the
   poison back to every consumer (the B1 ingest-resume path feeds it
   straight into synthesis).

Both are healed with the same repair passes production uses
(core/parsing/latex_repair). `quality_fixes` frontmatter is deliberately
left alone: its before/after snippets are a historical record of past
repairs and have no programmatic readers.

Usage:
    python scripts/repair_vault_control_chars.py                # dry-run
    python scripts/repair_vault_control_chars.py --apply \\
        --backup-dir /path/to/backups

Safe to run while the daemon is up: only markdown files are touched —
never ChromaDB. The daemon's VaultWatcher picks up each modified page
(60s debounce, single-writer busy lock), reindexes its chunks, and drops
its stale facets (chunks and facets share doc_id); the facet backfill
pump then re-signs facets from the now-clean appendix at zero LLM cost.
Do NOT pair this with a standalone reindexer — two ChromaDB writers
corrupt the database.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.append(str(Path(__file__).parent.parent.absolute()))

from core.parsing.latex_repair import (  # noqa: E402
    repair_latex_carriage_returns,
    repair_latex_escape_collisions,
)
from core.vault_utils import _FRONTMATTER_NL_RE  # noqa: E402

# Everything the repair passes can restore, plus lone CR for the residual
# report (a CR not followed by a known LaTeX suffix is left untouched).
_CONTROL_CHARS = {
    "\x08": "BS",
    "\x0b": "VT",
    "\x0c": "FF",
    "\x1b": "ESC",
    "\r": "CR",
}


def _count_control_chars(text: str) -> Counter[str]:
    return Counter(name for ch, name in _CONTROL_CHARS.items() for _ in range(text.count(ch)))


def repair_text(text: str) -> tuple[str, list[dict]]:
    """Apply both stock-repair passes; returns (cleaned, fixes)."""
    cleaned, fixes = repair_latex_escape_collisions(text)
    cleaned, cr_fixes = repair_latex_carriage_returns(cleaned)
    return cleaned, fixes + cr_fixes


def _repair_yaml_value(value: object, fixes: list[dict]) -> object:
    """Recursively repair every string in a YAML-loaded structure."""
    if isinstance(value, str):
        cleaned, new_fixes = repair_text(value)
        fixes.extend(new_fixes)
        return cleaned
    if isinstance(value, dict):
        return {k: _repair_yaml_value(v, fixes) for k, v in value.items()}
    if isinstance(value, list):
        return [_repair_yaml_value(v, fixes) for v in value]
    return value


def _count_yaml_control_chars(value: object) -> Counter[str]:
    """Count control chars in the DECODED strings of a YAML structure
    (a dump would escape them and count zero)."""
    counts: Counter[str] = Counter()
    if isinstance(value, str):
        counts.update(_count_control_chars(value))
    elif isinstance(value, dict):
        for v in value.values():
            counts.update(_count_yaml_control_chars(v))
    elif isinstance(value, list):
        for v in value:
            counts.update(_count_yaml_control_chars(v))
    return counts


def repair_frontmatter(text: str) -> tuple[str, list[dict], Counter[str]]:
    """Heal YAML-escaped control chars inside the `part_digest` block.

    Returns (new_text, fixes, residual) where residual counts control
    chars still present in the YAML-loaded part_digest after repair.
    """
    match = _FRONTMATTER_NL_RE.search(text)
    if not match:
        return text, [], Counter()
    try:
        data = yaml.safe_load(match.group(1))
    except Exception:
        return text, [], Counter()
    if not isinstance(data, dict) or "part_digest" not in data:
        return text, [], Counter()

    fixes: list[dict] = []
    repaired = _repair_yaml_value(data["part_digest"], fixes)
    residual = _count_yaml_control_chars(repaired)
    if not fixes:
        return text, [], residual

    data["part_digest"] = repaired
    # width=10**6 keeps long key points on one line instead of PyYAML's
    # default 80-col wrapping (backslash continuations inside math).
    new_fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10**6).strip()
    new_text = f"---\n{new_fm}\n---\n{text[match.end() :]}"
    return new_text, fixes, residual


def run(root: Path, apply: bool, backup_dir: Path | None) -> int:
    files_scanned = 0
    files_repaired = 0
    fix_counts: Counter[str] = Counter()
    residual: list[tuple[Path, Counter[str]]] = []

    for path in sorted(root.rglob("*.md")):
        if path.name.startswith("."):
            continue
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"SKIP (unreadable): {path}: {e}")
            continue

        cleaned, body_fixes = repair_text(text)
        cleaned, fm_fixes, fm_residual = repair_frontmatter(cleaned)
        fixes = body_fixes + fm_fixes

        # Raw count sees body residue; part_digest residue is YAML-escaped
        # in the raw text, so it is counted from the decoded strings.
        left = _count_control_chars(cleaned)
        left.update(fm_residual)
        if left:
            residual.append((path, left))
        if not fixes:
            continue

        files_repaired += 1
        for fix in fixes:
            fix_counts[fix["type"]] += 1
        rel = path.relative_to(root)
        detail = " + ".join(
            part
            for part in (
                f"{len(body_fixes)} body" if body_fixes else "",
                f"{len(fm_fixes)} frontmatter" if fm_fixes else "",
            )
            if part
        )
        print(f"{'FIX' if apply else 'WOULD FIX'} {rel}: {detail}")

        if apply:
            if backup_dir is not None:
                backup_path = backup_dir / rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup_path)
            path.write_text(cleaned, encoding="utf-8")

    print()
    print(
        f"Scanned {files_scanned} file(s); {files_repaired} repaired"
        f"{'' if apply else ' (dry-run)'}."
    )
    for fix_type, n in sorted(fix_counts.items()):
        print(f"  {fix_type}: {n}")
    if residual:
        print(
            f"\nResidual control chars after repair — {len(residual)} file(s) need manual review:"
        )
        for path, counts in residual:
            print(f"  {path.relative_to(root)}: {dict(counts)}")
    else:
        print("No residual control chars.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Vault directory to repair (default: PAGES_DIR from core.config)",
    )
    parser.add_argument("--apply", action="store_true", help="Write repairs (default: dry-run)")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Copy originals of repaired files here before writing (required with --apply)",
    )
    args = parser.parse_args()

    root = args.root
    if root is None:
        from core.config import PAGES_DIR

        root = PAGES_DIR
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    if args.apply and args.backup_dir is None:
        print("--apply requires --backup-dir (refusing to modify without backups)", file=sys.stderr)
        return 2

    return run(root.resolve(), args.apply, args.backup_dir)


if __name__ == "__main__":
    raise SystemExit(main())
