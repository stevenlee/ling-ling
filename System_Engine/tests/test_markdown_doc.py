"""core/markdown_doc.py — frontmatter+body roundtrip wrapper (P1)."""

from core.markdown_doc import MarkdownDocument


SAMPLE = """---
title: Sample Note
part_digest:
  thesis: entropy grows
tags:
  - physics
---

# Body heading

Content with a #hashtag inline.
"""


def test_from_text_splits_meta_and_body():
    doc = MarkdownDocument.from_text(SAMPLE)
    assert doc.meta["title"] == "Sample Note"
    assert doc.meta["part_digest"] == {"thesis": "entropy grows"}
    # Tags harvest BOTH frontmatter and body hashtags (parser semantics).
    assert "physics" in doc.meta["tags"]
    assert any("hashtag" in t for t in doc.meta["tags"])
    assert doc.body.startswith("# Body heading")
    assert "---" not in doc.body.split("\n")[0]


def test_roundtrip_does_not_grow_second_frontmatter(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(SAMPLE, encoding="utf-8")
    for _ in range(3):  # repeated read-modify-write must stay stable
        doc = MarkdownDocument.load(p)
        doc.meta["pending_concepts"] = "carry"
        doc.save()
    text = p.read_text(encoding="utf-8")
    assert text.count("title: Sample Note") == 1
    assert text.count("# Body heading") == 1
    reparsed = MarkdownDocument.from_text(text)
    assert reparsed.meta["pending_concepts"] == "carry"


def test_save_returns_written_text(tmp_path):
    p = tmp_path / "note.md"
    doc = MarkdownDocument({"title": "T"}, "body text", path=p)
    written = doc.save()
    assert written == p.read_text(encoding="utf-8")
    assert "title: T" in written and "body text" in written


def test_save_without_path_raises():
    doc = MarkdownDocument({}, "x")
    try:
        doc.save()
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_new_document_defaults():
    doc = MarkdownDocument()
    assert doc.meta == {} and doc.body == "" and doc.path is None
