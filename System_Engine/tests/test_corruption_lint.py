"""Corruption lint (DocQuality P4): zero-width strip, foreign-script warnings,
translation number fidelity. Fixtures are the artifacts observed live in the
published cloud_act output."""

from core.parsing.markdown_quality import (
    check_translation_number_fidelity,
    flag_foreign_scripts,
    repair_wikilink_newlines,
    run_markdown_quality_checks,
    strip_zero_width_chars,
)
from services.ingestion_pipeline import IngestionPipeline


class TestWikilinkNewline:
    def test_joins_line_wrapped_wikilink(self):
        # Observed live: a long link title wrapped mid-way in prose.
        text = "基礎 [[How Claude\nCode works... (Synthesis)]]。下一段"
        cleaned, fixes = repair_wikilink_newlines(text)
        assert "[[How Claude Code works... (Synthesis)]]" in cleaned
        assert "\n" not in cleaned[cleaned.index("[[") : cleaned.index("]]")]
        assert fixes and fixes[0]["type"] == "wikilink_newline"

    def test_collapses_indentation_around_wrap(self):
        cleaned, _ = repair_wikilink_newlines("[[Governed Shared Memory\n   for Multi-Agent]]")
        assert cleaned == "[[Governed Shared Memory for Multi-Agent]]"

    def test_clean_wikilink_untouched(self):
        text = "see [[A Normal Title (Synthesis)]] here"
        cleaned, fixes = repair_wikilink_newlines(text)
        assert cleaned == text and fixes == []

    def test_blank_line_not_treated_as_link(self):
        # `[[` and `]]` across a paragraph break is not a wrapped link — leave it.
        text = "[[start\n\nend]]"
        cleaned, fixes = repair_wikilink_newlines(text)
        assert cleaned == text and fixes == []

    def test_runs_inside_default_pipeline(self):
        cleaned, fixes = run_markdown_quality_checks("[[How Claude\nCode works (Synthesis)]]")
        assert "[[How Claude Code works (Synthesis)]]" in cleaned
        assert any(f["type"] == "wikilink_newline" for f in fixes)

    def test_idempotent(self):
        once, _ = repair_wikilink_newlines("[[A\nB (Part 3)]]")
        twice, fixes = repair_wikilink_newlines(once)
        assert twice == once and fixes == []


class TestZeroWidthStrip:
    def test_strips_u200b_inside_cjk(self):
        # Observed live: 「合法性法​法案」 — U+200B inside the statute name.
        text = "《澄清海外使用數據合法性法​法案》"
        cleaned, fixes = strip_zero_width_chars(text)
        assert "​" not in cleaned
        assert cleaned == "《澄清海外使用數據合法性法法案》"
        assert fixes[0]["type"] == "stripped_zero_width_chars"

    def test_strips_all_zero_width_variants(self):
        text = "a​b‌c‍d﻿e"
        cleaned, fixes = strip_zero_width_chars(text)
        assert cleaned == "abcde"
        assert "4 char(s)" in fixes[0]["before"]

    def test_clean_text_untouched(self):
        cleaned, fixes = strip_zero_width_chars("normal 中文 text")
        assert fixes == []

    def test_runs_inside_default_pipeline(self):
        cleaned, fixes = run_markdown_quality_checks("法​法案")
        assert "​" not in cleaned
        assert any(f["type"] == "stripped_zero_width_chars" for f in fixes)


class TestForeignScriptFlag:
    def test_flags_thai_run_inside_cjk(self):
        # Observed live: 「民事或刑事訴ทาง訴訟」 — Thai tokens inside a legal
        # translation.
        text = "作為針對依據本章提起之任何民事或刑事訴ทาง訴訟之完全抗辯。" + "正常中文內容。" * 30
        same, warnings = flag_foreign_scripts(text)
        assert same == text  # warning-only: never modifies
        assert len(warnings) == 1
        assert warnings[0]["type"] == "warning_foreign_script"
        assert "ทาง" in warnings[0]["before"]

    def test_substantial_foreign_text_is_intentional(self):
        # A document ABOUT Russian text must not be flooded with warnings.
        text = "Русский текст занимает большую часть документа.\n" * 10
        _, warnings = flag_foreign_scripts(text)
        assert warnings == []

    def test_clean_text_no_warnings(self):
        _, warnings = flag_foreign_scripts("純中文內容 with English words.")
        assert warnings == []


class TestNumberFidelity:
    _SOURCE = (
        "shall enter into force 180 days after notice under section 2523, "
        "reviewed every 5 years, filed within 14 days, "
        "signed at Budapest on November 23, 2001, under 50 U.S.C. 1801."
    )

    def test_corrupted_digits_flagged(self):
        # All three shapes observed live: 1180-for-180, split 252 3, 312-for-3124.
        body = "行政協議於通知國會 1180 天後生效。依第 252 條與第 312 條。"
        warnings = check_translation_number_fidelity(body, self._SOURCE)
        flagged = {w["before"] for w in warnings}
        assert flagged == {"1180", "252", "312"}
        assert all(w["type"] == "warning_number_not_in_source" for w in warnings)

    def test_faithful_numbers_pass(self):
        body = "於 180 天後生效；每 5 年審查；14 日內提出；50 U.S.C. 1801。"
        assert check_translation_number_fidelity(body, self._SOURCE) == []

    def test_month_name_becomes_month_number(self):
        body = "於 2001 年 11 月 23 日簽署。"
        assert check_translation_number_fidelity(body, self._SOURCE) == []

    def test_fenced_code_and_wikilinks_ignored(self):
        body = (
            '```mermaid\nquadrantChart\n  "點": [0.75, 0.99]\n```\n[[cloud_act (Part 99)|下一篇]]\n'
        )
        assert check_translation_number_fidelity(body, self._SOURCE) == []

    def test_single_digits_tolerated(self):
        assert check_translation_number_fidelity("第 7 項與第 9 款。", self._SOURCE) == []

    def test_comma_grouping_normalized(self):
        warnings = check_translation_number_fidelity("金額為 1,801 元。", self._SOURCE)
        assert warnings == []  # 1,801 == 1801 in source

    def test_each_number_warned_once(self):
        body = "1180 天。再次提到 1180 天。"
        warnings = check_translation_number_fidelity(body, self._SOURCE)
        assert len(warnings) == 1


class TestNumberFidelityHexColors:
    def test_hex_color_digits_not_flagged_even_outside_fence(self):
        # Second re-run regression: an orphan leading fence inverted parity,
        # so mermaid style lines were scanned as prose and their hex colors
        # (#28a745, corrupted ＃dc3545) flagged as numbers. Hex literals are
        # stripped before token extraction regardless of fence state.
        body = "style K fill:#f8d7da,stroke ＃dc3545,stroke-width:2px\nstyle J fill:#d4edda,stroke:#28a745"
        assert check_translation_number_fidelity(body, "source with 180 days") == []


class TestOrphanLeadingFence:
    def test_orphan_closer_before_tagged_fence_removed(self):
        # Second re-run regression (cloud_act Part 2): the model fenced only
        # its frontmatter; the stray closer survived as the first body line
        # and inverted every later fence's parity.
        from core.parsing.markdown_quality import strip_orphan_leading_fence

        body = "```\n\n# 標題\n\n```mermaid\ngraph TD\n    A --> B\n```\n"
        cleaned, fixes = strip_orphan_leading_fence(body)
        assert not cleaned.startswith("```")
        assert "```mermaid" in cleaned
        assert fixes[0]["type"] == "stripped_orphan_leading_fence"

    def test_legitimate_leading_code_block_kept(self):
        from core.parsing.markdown_quality import strip_orphan_leading_fence

        body = "```\nplain code block\n```\n\nprose after\n"
        cleaned, fixes = strip_orphan_leading_fence(body)
        assert cleaned == body
        assert fixes == []

    def test_pipeline_restores_fence_parity(self):
        body = "```\n\n# 標題\n\n```mermaid\ngraph TD\n    A --> B\n```\n"
        cleaned, fixes = run_markdown_quality_checks(body)
        assert cleaned.count("```") % 2 == 0
        assert any(f["type"] == "stripped_orphan_leading_fence" for f in fixes)


class TestMermaidStyleHash:
    def test_fullwidth_hash_and_missing_colon_repaired(self):
        # Verbatim from the cloud_act second re-run (2/34 parse failures).
        from core.parsing.mermaid_repair import repair_mermaid_style_hash

        text = (
            "```mermaid\n"
            "graph TD\n"
            '    K["節點"]\n'
            "    style K fill:#f8d7da,stroke ＃dc3545,stroke-width:2px\n"
            "```"
        )
        fixed, fixes = repair_mermaid_style_hash(text)
        assert "stroke:#dc3545" in fixed
        assert "＃" not in fixed
        assert fixes[0]["type"] == "repaired_mermaid_style_hash"

    def test_fullwidth_hash_outside_fence_untouched(self):
        from core.parsing.mermaid_repair import repair_mermaid_style_hash

        text = "散文提到全形井號 ＃hashtag 不該被改。"
        fixed, fixes = repair_mermaid_style_hash(text)
        assert fixed == text
        assert fixes == []

    def test_in_default_pipeline(self):
        text = (
            "```mermaid\n"
            "graph TD\n"
            '    K["節點"]\n'
            "    style K fill:#f8d7da,stroke ＃dc3545,stroke-width:2px\n"
            "```"
        )
        cleaned, fixes = run_markdown_quality_checks(text)
        assert "stroke:#dc3545" in cleaned


class TestWarningRouting:
    def test_split_quality_warnings(self):
        fixes = [
            {"type": "quoted_mermaid_labels", "line": 3},
            {"type": "warning_foreign_script", "line": 9, "before": "訴ทาง訴訟"},
            "legacy string fix",
        ]
        applied, warnings = IngestionPipeline._split_quality_warnings(fixes)
        assert applied == [{"type": "quoted_mermaid_labels", "line": 3}, "legacy string fix"]
        assert warnings == [{"type": "warning_foreign_script", "line": 9, "before": "訴ทาง訴訟"}]
