"""Transform curated review notes (lings-desktop/Blog/) into Quartz content.

Reviews authored by @ling-review and dropped into Blog/ are turned into
web-ready Markdown for a Quartz digital garden:

  - strip engine/internal frontmatter → emit clean Quartz frontmatter
    (title / date / tags / draft / aliases / description);
  - wikilink boundary policy: a `[[link]]` to another *published* review is
    kept (so the graph / backlinks connect); every other `[[link]]` is stripped
    to plain text (never leak or 404 into the private vault);
  - the leading H1 becomes the frontmatter title and is removed from the body
    (Quartz renders the title itself — avoids a duplicated heading);
  - `== ==` highlights, Mermaid and callouts are left untouched — Quartz
    renders them natively.

Pure functions + a thin CLI. The future `@ling-publish` agent imports the same
functions; nothing here touches the LLM, ChromaDB, or the running daemon.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_FENCED_YAML_RE = re.compile(r"^```ya?ml\s*\n(.*?)\n```\s*", re.DOTALL)
_RAW_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_ONELINER_RE = re.compile(r"^\*\*(?:一句話[^：:*]*|In one line)[：:]\*\*\s*(.+?)\s*$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(meta, body) — accepts a fenced ```yaml block or a raw --- block, or neither."""
    m = _FENCED_YAML_RE.match(text) or _RAW_FM_RE.match(text)
    if not m:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():].strip()


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w一-鿿-]+", "-", s)  # keep word chars + CJK
    return re.sub(r"-+", "-", s).strip("-")


def work_name(meta: dict, path: Path) -> str:
    """The reviewed work's name — the node identity other reviews link to."""
    return str(meta.get("target") or path.stem).strip()


def _description(body: str) -> str:
    m = _ONELINER_RE.search(body)
    return m.group(1).strip() if m else ""


def _tags(meta: dict) -> list[str]:
    tags = ["review"]
    genre = meta.get("genre")
    if isinstance(genre, str) and genre:
        tags.append(genre)
    for t in meta.get("tags") or []:
        if isinstance(t, str):
            tags.append(t)
    return list(dict.fromkeys(tags))  # de-dup, keep order


def rewrite_wikilinks(body: str, publish_slugs: set[str]) -> str:
    """Keep `[[X]]` when X is a published review; otherwise strip to plain text."""
    def repl(m: re.Match) -> str:
        target, _, alias = m.group(1).partition("|")
        if slugify(target) in publish_slugs:
            return m.group(0)
        return (alias or target).strip()
    return _WIKILINK_RE.sub(repl, body)


def transform_review(text: str, path: Path, publish_slugs: set[str], date: str) -> tuple[str, str]:
    """Return (output_slug, full_markdown) — web-ready Quartz page for one review."""
    meta, body = split_frontmatter(text)
    work = work_name(meta, path)

    h1 = _H1_RE.search(body)
    title = h1.group(1).strip() if h1 else work
    if h1:  # title moves to frontmatter; drop the duplicate heading from the body
        body = (body[:h1.start()] + body[h1.end():]).strip()

    fm = {
        "title": title,
        "date": date,
        "tags": _tags(meta),
        "draft": False,
        "aliases": [work],          # so `[[work]]` from other reviews resolves here
        "description": _description(body),
    }
    body = rewrite_wikilinks(body, publish_slugs)
    front = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    return slugify(work), f"---\n{front}\n---\n\n{body}\n"


def publish_blog(blog_dir: Path, content_dir: Path, date: str) -> list[Path]:
    """Transform every review in blog_dir → content_dir. Files starting with
    '_' are skipped. Returns the written paths."""
    blog_dir, content_dir = Path(blog_dir), Path(content_dir)
    sources = sorted(f for f in blog_dir.glob("*.md") if not f.name.startswith("_"))

    # First pass: the publish set (slugs of all work names) drives link policy.
    parsed = [(f, f.read_text(encoding="utf-8")) for f in sources]
    publish_slugs = {slugify(work_name(split_frontmatter(t)[0], f)) for f, t in parsed}

    content_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for f, text in parsed:
        slug, doc = transform_review(text, f, publish_slugs, date)
        out = content_dir / f"{slug}.md"
        out.write_text(doc, encoding="utf-8")
        written.append(out)
    return written


def main() -> None:
    import argparse
    from datetime import date as _date

    # repo_root/lings-desktop/Blog, computed from this file's location — no
    # dependency on the engine's config, so this runs as a plain script:
    #   python services/blog_transform.py --content <quartz>/content
    default_blog = Path(__file__).resolve().parents[2] / "lings-desktop" / "Blog"

    ap = argparse.ArgumentParser(description="Publish curated reviews to a Quartz content/ dir.")
    ap.add_argument("--blog", default=str(default_blog), help="curation folder")
    ap.add_argument("--content", required=True, help="Quartz repo content/ dir")
    ap.add_argument("--date", default=_date.today().isoformat(), help="publish date (YYYY-MM-DD)")
    args = ap.parse_args()

    written = publish_blog(Path(args.blog), Path(args.content), args.date)
    print(f"Published {len(written)} page(s) → {args.content}")
    for p in written:
        print("  -", p.name)


if __name__ == "__main__":
    main()
