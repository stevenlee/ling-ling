"""Fence-aware section splitting for packed-code notes (pack_code output).

A packed note's body is `## <path>` sections, each holding one fenced code
block. The naive `^## ` regex split treated a top-column `## comment` INSIDE a
fence as a section boundary — confirmed to shear a review chunk in two and,
worse, to drop a module from the architecture facts entirely (the severed
section loses its closing fence, so the python-block regex stops matching).

Two modes, strongest first:

* **Whitelist** (``known_labels`` given — the packed note's own frontmatter
  ``source_paths``): a `## ` line is a boundary ONLY if its label is one of the
  packed paths. pack_code writes headings and source_paths from the same list,
  so this is exact — and immune even to fenced content that happens to contain
  top-column ``` or `## ` lines.
* **Fence-tracking fallback** (no whitelist): a `## ` line is a boundary only
  while outside a ``` fence. Top-column ``` inside a *string* (e.g. a docstring
  holding a fenced example) can still fool this mode — hence the whitelist is
  preferred whenever frontmatter is available.
"""

from __future__ import annotations


def split_sections(body: str, known_labels: list[str] | None = None) -> list[tuple[str, str]]:
    """Split a packed-code body into ``(file_label, section_text)`` pairs.

    ``section_text`` includes its ``## label`` heading line. Content before the
    first heading (the packed note's title line) is ignored.
    """
    known = set(known_labels) if known_labels else None
    sections: list[tuple[str, list[str]]] = []
    label: str | None = None
    acc: list[str] = []
    in_fence = False

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        else:
            is_heading = line.startswith("## ") and len(line) > 3 and not line[3].isspace()
            if is_heading:
                is_boundary = line[3:].strip() in known if known is not None else not in_fence
                if is_boundary:
                    if label is not None:
                        sections.append((label, acc))
                    label = line[3:].strip()
                    acc = [line]
                    continue
        if label is not None:
            acc.append(line)

    if label is not None:
        sections.append((label, acc))
    return [(lbl, "\n".join(ls)) for lbl, ls in sections]
