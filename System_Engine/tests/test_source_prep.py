"""flatten_linenumber_tables (source_prep 0d) — OCR line-number tables → prose.

Golden fixtures are verbatim excerpts from raw/consolidate/cloud_act.md, the
document whose 671 fake table rows motivated the pass (DocQuality P2).
"""

from services.source_prep import flatten_linenumber_tables

# Two consecutive page tables (page break between), with every artifact the
# real converter produces: <br> glue, cross-row word splits (`com` +
# `munications`), hyphen+space wraps (`dis` row ends), enumerator cues.
_CLOUD_ACT_EXCERPT = """# **DIVISION V—CLOUD ACT**

- (4) Communications-service providers face po- tential conflicting legal obligations.

| 1  | United States law may prohibit providers from dis                    |
|----|----------------------------------------------------------------------|
| 2  | closing.                                                             |
| 3  | (5) Foreign law may create similarly conflicting                     |
| 4  | legal<br>obligations<br>when<br>chapter<br>121<br>of<br>title<br>18, |
| 5  | United States Code, requires disclosure of                           |
| 6  | electronic data that foreign law prohibits com                       |
| 7  | munications-service providers from disclosing.                       |

| 1  | (6) International agreements provide a mecha                         |
| 2  | nism for resolving these potential conflicting legal                 |
| 3  | obligations.                                                         |
"""


class TestFlattenLineNumberTables:
    def test_tables_become_prose(self):
        out, info = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        assert info == ["flattened 2 line-number tables"]
        assert not any(line.startswith("|") for line in out.splitlines())

    def test_br_glue_removed(self):
        out, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        assert "<br>" not in out
        assert "legal obligations when chapter 121 of title 18," in out

    def test_cross_row_word_splits_rejoined_via_vocab(self):
        out, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        # `dis` + `closing.` and `com` + `munications-service` rejoin because
        # "disclosing" / "communications" appear intact elsewhere in the doc.
        assert "from disclosing." in out
        assert "prohibits communications-service providers" in out
        # `mecha` + `nism` rejoins via "mechanism"? Not in vocab elsewhere —
        # falls back to a space join, never a silent drop.
        assert "mecha nism" in out or "mechanism" in out

    def test_hyphen_space_wrap_repaired_outside_tables_too(self):
        out, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        # Bullet-list prose has the same artifact; "potential" appears intact
        # in the second table so the vocab check rejoins it.
        assert "face potential conflicting" in out

    def test_enumerator_starts_new_paragraph(self):
        out, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        assert "\n\n(5) Foreign law" in out
        assert "\n\n(6) International agreements" in out

    def test_page_break_between_tables_merges_flow(self):
        # The (6) paragraph continues right after the (5) paragraph — no
        # leftover table scaffolding between them.
        out, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        i5, i6 = out.index("(5) Foreign law"), out.index("(6) International")
        assert "|" not in out[i5:i6]

    def test_idempotent(self):
        once, _ = flatten_linenumber_tables(_CLOUD_ACT_EXCERPT)
        twice, info = flatten_linenumber_tables(once)
        assert twice == once
        assert info == []

    def test_real_data_table_untouched(self):
        doc = (
            "# Report\n\n"
            "| Quarter | Revenue |\n"
            "|---------|---------|\n"
            "| Q1      | 100     |\n"
            "| Q2      | 120     |\n"
            "| Q3      | 90      |\n"
        )
        out, info = flatten_linenumber_tables(doc)
        assert out == doc
        assert info == []

    def test_numeric_first_column_but_jumping_untouched(self):
        # Numbers that jump around are data (amounts), not line numbers.
        doc = (
            "| 500 | Alpha |\n|-----|-------|\n| 20  | Beta  |\n| 300 | Gamma |\n| 40  | Delta |\n"
        )
        out, info = flatten_linenumber_tables(doc)
        assert out == doc
        assert info == []

    def test_standalone_short_table_untouched_without_strict_evidence(self):
        # A lone 1-row numeric table in an otherwise normal doc is NOT
        # flattened — the relaxed rule needs a strictly-detected table first.
        doc = "Intro text.\n\n| 2 | SEC. 101. SHORT TITLE. |\n|---|---|\n\nMore text.\n"
        out, info = flatten_linenumber_tables(doc)
        assert out == doc
        assert info == []

    def test_short_straggler_flattened_when_doc_has_strict_tables(self):
        doc = "| 2 | SEC. 101. SHORT TITLE. |\n|---|---|\n\n" + _CLOUD_ACT_EXCERPT
        out, info = flatten_linenumber_tables(doc)
        assert "SEC. 101. SHORT TITLE." in out
        assert not any(line.startswith("|") for line in out.splitlines())
        assert info == ["flattened 3 line-number tables"]

    def test_year_row_not_mistaken_for_line_number(self):
        # Even in a confirmed line-number doc, a short table whose number is
        # too big to be a page line (a year, an amount) stays a table.
        doc = _CLOUD_ACT_EXCERPT + "\n| 2024 | Annual revenue |\n|---|---|\n"
        out, _ = flatten_linenumber_tables(doc)
        assert "| 2024 | Annual revenue |" in out

    def test_suspended_hyphen_preserved(self):
        doc = _CLOUD_ACT_EXCERPT + "\nApplies to pre- and post-enactment data.\n"
        out, _ = flatten_linenumber_tables(doc)
        assert "pre- and post-enactment" in out

    def test_frontmatter_preserved(self):
        doc = "---\ntitle: x\n---\n" + _CLOUD_ACT_EXCERPT
        out, _ = flatten_linenumber_tables(doc)
        assert out.startswith("---\ntitle: x\n---\n")

    def test_no_tables_fast_path(self):
        doc = "Just prose, no pipes at all.\n"
        out, info = flatten_linenumber_tables(doc)
        assert out == doc
        assert info == []
