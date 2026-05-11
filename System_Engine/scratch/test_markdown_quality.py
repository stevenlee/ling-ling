from core.parser import run_markdown_quality_checks


def test_wraps_bare_mermaid_until_markdown_boundary():
    source = """## Flow
mermaid
graph TD
A[Start] --> B[End]

---
## Next
Body"""

    fixed, fixes = run_markdown_quality_checks(source)

    assert "wrapped_bare_mermaid" in fixes
    assert '```mermaid\ngraph TD\nA["Start"] --> B["End"]\n```' in fixed
    assert "```\n---\n## Next" in fixed


def test_does_not_swallow_prose_after_bare_mermaid():
    source = """## Flow
mermaid
graph TD
A[Start] --> B[End]

This paragraph should stay outside."""

    fixed, fixes = run_markdown_quality_checks(source)

    assert "wrapped_bare_mermaid" in fixes
    assert "```\nThis paragraph should stay outside." in fixed


def test_preserves_existing_mermaid_fence_and_quotes_label():
    source = """```mermaid
graph TD
A --> B
C[Plain Label]
```

Text"""

    fixed, fixes = run_markdown_quality_checks(source)

    assert fixes == ["quoted_mermaid_labels"]
    assert 'C["Plain Label"]' in fixed


def test_quotes_mermaid_labels_with_punctuation():
    source = """```mermaid
graph TD
A[Start] --> D[階段一: 投入期 (過去 3 年)]
D --> E{"是否通過？"}
E --> F[abc "def" ghi (jk)]
F --> G["已經正確: 不要重包"]
```"""

    fixed, fixes = run_markdown_quality_checks(source)

    assert "quoted_mermaid_labels" in fixes
    assert 'A["Start"] --> D["階段一: 投入期 (過去 3 年)"]' in fixed
    assert 'E{"是否通過？"}' in fixed
    assert 'F["abc \\"def\\" ghi (jk)"]' in fixed
    assert 'G["已經正確: 不要重包"]' in fixed


def test_strips_accidental_body_frontmatter():
    source = """---
title: Bad
---

### 核心命題
Body"""

    fixed, fixes = run_markdown_quality_checks(source, strip_frontmatter=True)

    assert fixes == ["removed_body_frontmatter"]
    assert fixed == "### 核心命題\nBody"


def test_repairs_latex_carriage_return_arrow():
    source = "知識循環：閱讀 $" + "\r" + "ightarrow$ 判斷"

    fixed, fixes = run_markdown_quality_checks(source)

    assert "repaired_latex_carriage_return" in fixes
    assert fixed == r"知識循環：閱讀 $\rightarrow$ 判斷"


if __name__ == "__main__":
    test_wraps_bare_mermaid_until_markdown_boundary()
    test_does_not_swallow_prose_after_bare_mermaid()
    test_preserves_existing_mermaid_fence_and_quotes_label()
    test_quotes_mermaid_labels_with_punctuation()
    test_strips_accidental_body_frontmatter()
    test_repairs_latex_carriage_return_arrow()
    print("markdown quality tests passed")
