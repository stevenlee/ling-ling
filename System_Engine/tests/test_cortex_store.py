"""cortex_store: the only Cortex page read/write path. The hard gate is
parse(render(page)) round-tripping every field — including Chinese
claims, special characters, and PyYAML's eager timestamp parsing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from services.cortex_store import (
    CortexPage,
    claim_filename,
    load_all_pages,
    make_claim_id,
    parse_cortex_page,
    render_cortex_page,
    save_cortex_page,
)


def _page(tmp_path, claim, **kw) -> CortexPage:
    claim_id = make_claim_id(claim)
    defaults = dict(
        claim_id=claim_id,
        path=claim_filename(claim, claim_id, tmp_path),
        claim=claim,
        confidence=0.5,
        S=1,
        last_reinforced_at="2026-06-11T03:12:00",
        created="2026-06-11T03:12:00",
        updated="2026-06-11T03:12:00",
    )
    defaults.update(kw)
    return CortexPage(**defaults)


def _roundtrip(page: CortexPage) -> CortexPage:
    save_cortex_page(page)
    parsed = parse_cortex_page(page.path)
    assert parsed is not None
    return parsed


class TestRoundTrip:
    def test_minimal_page(self, tmp_path):
        page = _page(tmp_path, "記憶是重建性的，每次回想都會改寫痕跡。")
        assert _roundtrip(page) == page

    def test_full_page_with_evidence_variants_counterpoints(self, tmp_path):
        page = _page(
            tmp_path,
            "Spaced repetition outperforms massed practice for retention.",
            confidence=0.7,
            S=3,
            evidence=[
                {
                    "insight": "[20260611-031200][Vault][full-insight].md",
                    "sources": ["Doc A", "中文文件 B"],
                    "date": "2026-06-11",
                    "summary": "一行摘要：含「特殊」字元 & 符號 <ok>",
                },
                {
                    "insight": "second.md",
                    "sources": [],
                    "date": "2026-06-10",
                    "summary": "",
                },
            ],
            contradictions=["cortex-aaaa111122223333"],
            related=["cortex-bbbb444455556666"],
            variants=["每次提取都會再固化記憶", "Recall rewrites the trace"],
            counterpoints=["過度泛化到程序性記憶的證據不足"],
        )
        assert _roundtrip(page) == page

    def test_yaml_timestamp_coercion(self, tmp_path):
        """PyYAML parses ISO strings into datetime objects; the parser must
        coerce them back to strings or round-trip breaks."""
        page = _page(tmp_path, "Timestamps stay opaque strings here.")
        save_cortex_page(page)
        text = page.path.read_text(encoding="utf-8")
        # Frontmatter timestamps are unquoted → PyYAML would eagerly parse.
        parsed = parse_cortex_page(page.path)
        assert isinstance(parsed.last_reinforced_at, str)
        assert isinstance(parsed.evidence, list)
        assert parsed == page
        assert "2026-06-11T03:12:00" in text

    def test_evidence_date_coercion(self, tmp_path):
        page = _page(
            tmp_path, "Evidence dates also stay strings.",
            evidence=[{"insight": "x.md", "sources": ["A"], "date": "2026-06-11", "summary": "s"}],
        )
        parsed = _roundtrip(page)
        assert parsed.evidence[0]["date"] == "2026-06-11"
        assert isinstance(parsed.evidence[0]["date"], str)


class TestParseDefenses:
    def test_missing_claim_id_returns_none(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text("---\nstatus: active\n---\n\n## Core Claim\nclaim\n", encoding="utf-8")
        assert parse_cortex_page(bad) is None

    def test_missing_core_claim_returns_none(self, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_text("---\nclaim_id: cortex-x\n---\n\nNo sections.\n", encoding="utf-8")
        assert parse_cortex_page(bad) is None

    def test_unreadable_file_returns_none(self, tmp_path):
        assert parse_cortex_page(tmp_path / "ghost.md") is None


class TestFilenameAndLoad:
    def test_filename_sanitized_and_collision_suffixed(self, tmp_path):
        claim = "Claims: with *markdown* and / slashes [[links]]!"
        claim_id = make_claim_id(claim)
        p1 = claim_filename(claim, claim_id, tmp_path)
        assert "/" not in p1.stem and "*" not in p1.name and "[" not in p1.name
        p1.write_text("x", encoding="utf-8")
        p2 = claim_filename(claim, claim_id, tmp_path)
        assert p1 != p2
        assert claim_id[-6:] in p2.stem

    def test_load_all_skips_underscore_and_invalid(self, tmp_path):
        good = _page(tmp_path, "A valid consolidated claim.")
        save_cortex_page(good)
        (tmp_path / "_README.md").write_text("not a page", encoding="utf-8")
        (tmp_path / "broken.md").write_text("no frontmatter at all", encoding="utf-8")

        pages = load_all_pages(tmp_path)
        assert [p.claim_id for p in pages] == [good.claim_id]
