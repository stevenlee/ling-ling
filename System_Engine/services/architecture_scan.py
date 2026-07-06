"""Deterministic architecture facts from a packed-code note (C3).

@ling-architect feeds these facts to the LLM so a weak model (gemma4:26b) draws
the diagram by TRANSCRIBING structure, not guessing it. Zero LLM: pure `ast`.

For each `## <path>` section's Python block we extract the module's top-level
classes/functions and its imports, split internal vs external. "Internal" is
derived from the packed note's own `source_paths` — an import whose root package
is one of the packed tree's package dirs — so the scan is self-contained and
testable without touching the repo filesystem.
"""

from __future__ import annotations

import ast
import re

import yaml

from services.packed_note import split_sections

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_PY_BLOCK_RE = re.compile(r"```python\s*\n(.*?)\n```", re.DOTALL)


def _internal_roots(source_paths: list[str]) -> set[str]:
    """Package roots of the packed tree. For `System_Engine/services/x.py` the
    root is `services` (the component after a leading `System_Engine` wrapper),
    else the first component."""
    roots: set[str] = set()
    for p in source_paths:
        parts = [c for c in p.replace("\\", "/").split("/") if c]
        if not parts:
            continue
        if len(parts) >= 2 and parts[0] == "System_Engine":
            roots.add(parts[1])
        else:
            roots.add(parts[0])
    return roots


def _classify_imports(tree: ast.Module, internal_roots: set[str]) -> tuple[list[str], list[str]]:
    internal: list[str] = []
    external: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                (internal if mod.split(".")[0] in internal_roots else external).append(mod)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — always internal
                internal.append("." * node.level + (node.module or ""))
                continue
            mod = node.module or ""
            root = mod.split(".")[0]
            (internal if root in internal_roots else external).append(mod)
    # de-dup, preserve order
    return _dedup(internal), _dedup(external)


def _dedup(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def scan_architecture(packed_text: str) -> dict:
    """Return {"internal_roots": [...], "modules": [{path, classes, functions,
    imports_internal, imports_external}]}. Sections whose Python fails to parse
    are reported with a `parse_error` flag rather than dropped."""
    m = _FRONTMATTER_RE.match(packed_text)
    source_paths: list[str] = []
    body = packed_text
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
            sp = fm.get("source_paths")
            if isinstance(sp, list):
                source_paths = [str(x) for x in sp]
        except yaml.YAMLError:
            pass
        body = packed_text[m.end() :]

    internal_roots = _internal_roots(source_paths)
    modules: list[dict] = []
    # Fence-aware, whitelist-first splitting (packed_note) — a top-column `## `
    # comment inside a fence used to sever the section, losing its closing
    # fence and silently dropping the whole module from the facts.
    for path, sec in split_sections(body, source_paths):
        cb = _PY_BLOCK_RE.search(sec)
        if not cb:
            continue
        code = cb.group(1)
        try:
            tree = ast.parse(code)
        except SyntaxError:
            modules.append({"path": path, "parse_error": True})
            continue
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        functions = [
            n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        imports_internal, imports_external = _classify_imports(tree, internal_roots)
        modules.append(
            {
                "path": path,
                "classes": classes,
                "functions": functions,
                "imports_internal": imports_internal,
                "imports_external": imports_external,
            }
        )
    return {"internal_roots": sorted(internal_roots), "modules": modules}


def format_facts(scan: dict) -> str:
    """Render the scan as a compact facts table for the LLM context."""
    lines = ["# 結構事實(由 ast 抽取,請據此作圖,勿臆測)\n"]
    for mod in scan.get("modules", []):
        lines.append(f"## {mod['path']}")
        if mod.get("parse_error"):
            lines.append("- (無法解析此檔的 Python,略過)")
            continue
        if mod.get("classes"):
            lines.append(f"- classes: {', '.join(mod['classes'])}")
        if mod.get("functions"):
            lines.append(f"- functions: {', '.join(mod['functions'])}")
        if mod.get("imports_internal"):
            lines.append(f"- 內部依賴: {', '.join(mod['imports_internal'])}")
        if mod.get("imports_external"):
            lines.append(f"- 外部依賴: {', '.join(mod['imports_external'])}")
        lines.append("")
    return "\n".join(lines)
