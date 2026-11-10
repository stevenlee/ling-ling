"""Section-addressed Q&A: "§2.1 of <doc>" loads the Parts that hold §2.1."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

from unittest.mock import MagicMock

import pytest

from services import section_locator
from services.section_locator import heading_label, locate_section_parts, parse_section_refs

DOC = "Proving Things About JEPA Representations"

# Raw source as archived at ingestion. "  " mirrors the real paper's
# heading spacing. §2.2 is short enough to start and end inside one Part.
RAW = (
    f"## {DOC}\n\nPreamble text.\n\n"
    "## 1  Introduction\n\nIntro body.\n\n"
    "## 2  Shared Positives\n\nChapter two lead.\n\n"
    "### 2.1  The Theorem\n\nTheorem setup.\n\n"
    "#### World and pairs.\n\nMore theorem detail.\n\n"
    "```python\n# 2.1 not a heading inside a fence\n```\n\n"
    "Theorem conclusion.\n\n"
    "### 2.2  When Positives Differ\n\nShort section.\n\n"
    "### 2.3  Views\n\nViews body text that goes on.\n\n"
    "## 12  Later Chapter\n\nTwelve body.\n\n"
    "## Appendix A Proofs\n\nProof lead.\n\n"
    "### A.1  Notation\n\nNotation body.\n"
)


def _cut(marker: str) -> int:
    return RAW.index(marker)


# Part boundaries (char offsets into RAW) and the END-boundary section_path the
# splitter records for each Part.
_BOUNDS = [
    (0, _cut("## 2 "), ["Intro", "1  Introduction"]),
    (
        _cut("## 2 "),
        _cut("#### World"),
        ["Intro", "2  Shared Positives", "2.1  The Theorem"],
    ),
    (
        _cut("#### World"),
        _cut("Theorem conclusion."),
        ["Intro", "2  Shared Positives", "2.1  The Theorem", "World and pairs."],
    ),
    (
        _cut("Theorem conclusion."),
        _cut("Views body"),
        ["Intro", "2  Shared Positives", "2.3  Views"],
    ),
    (_cut("Views body"), _cut("## 12"), ["Intro", "2  Shared Positives", "2.3  Views"]),
    (_cut("## 12"), _cut("## Appendix"), ["Intro", "12  Later Chapter"]),
    (_cut("## Appendix"), len(RAW), ["Intro", "Appendix A Proofs", "A.1  Notation"]),
]


def _write_part(folder, n, start, end, section_path, *, spans=True):
    lines = ["---", f"title: {DOC} (Part {n})", f"part: {n}"]
    if spans:
        lines += [f"source_start_char: {start}", f"source_end_char: {end}"]
    lines.append("section_path:")
    lines += [f"- '{level}'" for level in section_path]
    lines += ["---", "", f"# Part {n} body", "", f"PART-{n}-TEXT"]
    (folder / f"{DOC} (Part {n}).md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    pages, raw = tmp_path / "pages", tmp_path / "raw"
    folder = pages / DOC
    folder.mkdir(parents=True)
    raw.mkdir()
    (raw / f"{DOC}.md").write_text(RAW, encoding="utf-8")
    for n, (start, end, path) in enumerate(_BOUNDS, start=1):
        _write_part(folder, n, start, end, path)
    (pages / "Unrelated Short").mkdir()
    monkeypatch.setattr(section_locator, "PAGES_DIR", pages)
    monkeypatch.setattr(section_locator, "RAW_CONSOLIDATE_DIR", raw)
    monkeypatch.setattr(section_locator.settings, "SECTION_LOCATE_MAX_PARTS", 4)
    return {"pages": pages, "raw": raw, "folder": folder}


def _parts(question, explicit=None, **kw):
    parts, total = locate_section_parts(question, explicit or [], **kw)
    return [p.part for p in parts], total


# ── parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question, labels",
    [
        ("請解釋 §2.1 的定理", ["2.1"]),
        ("see § 3.2.1 please", ["3.2.1"]),
        ("What does Section 2 claim?", ["2"]),
        ("in sec. 4 they show", ["4"]),
        ("第 2.1 節講什麼", ["2.1"]),
        ("第3章的重點", ["3"]),
        ("Appendix A proofs", ["a"]),
        ("附錄 B 的推導", ["b"]),
        ("§A.1 notation", ["a.1"]),
        ("§2.1 and §2.1 again, plus §2.2", ["2.1", "2.2"]),
    ],
)
def test_parse_section_refs(question, labels):
    assert parse_section_refs(question) == labels


@pytest.mark.parametrize(
    "question",
    ["upgrade to version 2.1", "Python 3.12 改了什麼", "這份檔案 2.1 GB", "the A team", ""],
)
def test_parse_ignores_numbers_without_a_section_marker(question):
    assert parse_section_refs(question) == []


@pytest.mark.parametrize(
    "heading, label",
    [
        ("2.1  The Platonic Representation Theorem", "2.1"),
        ("Appendix A Proofs", "a"),
        ("A.1  Notation and Objectives", "a.1"),
        ("Section 4 Results", "4"),
        ("第 3 章 結論", "3"),
        ("12.1 Foo", "12.1"),
        ("A Framework for Frontier AI", None),
        ("Objectives of the form ().", None),
    ],
)
def test_heading_label(heading, label):
    assert heading_label(heading) == label


# ── locating ──────────────────────────────────────────────────────────────


def test_replayed_question_locates_the_section_not_the_appendix(vault):
    # Live 2026-11-06: the title was typed with different spacing and no link,
    # and similarity retrieval returned the appendix Part instead of §2.1.
    question = "根據 Proving Things About JEPA  Representations 的 §2.1，在哪些假設下成立？請引用。"

    parts, total = _parts(question)

    assert parts == [2, 3, 4]
    assert total == 3
    assert 7 not in parts


def test_short_section_inside_one_part_is_found_from_raw_spans(vault):
    # §2.2 starts and ends inside Part 4, whose END-boundary section_path says
    # 2.3 — the section_path alone cannot see it.
    assert _parts(f"§2.2 of [[{DOC}]]") == ([4], 1)
    assert (
        section_locator._locate_by_section_path(
            section_locator._part_pages(vault["folder"]), ["2.2"]
        )
        == []
    )


def test_chapter_label_does_not_match_a_longer_number(vault):
    parts, _ = _parts(f"Section 2 of [[{DOC}]]")
    assert 6 not in parts  # chapter 12
    assert parts == [2, 3, 4, 5]


def test_fenced_code_comment_is_not_a_heading(vault):
    spans = section_locator._section_spans(RAW, ["2.1"])
    assert [label for _, _, label, _ in spans] == ["2.1"]


def test_appendix_and_lettered_subsection(vault):
    assert _parts(f"[[{DOC}]] Appendix A") == ([7], 1)
    assert _parts(f"[[{DOC}]] §A.1") == ([7], 1)


def test_part_link_resolves_to_its_document(vault):
    assert _parts(f"[[{DOC} (Part 9)]] 的 §2.1")[0] == [2, 3, 4]


def test_cap_reports_total_before_truncation(vault):
    parts, total = _parts(f"Section 2 of [[{DOC}]]", max_parts=2)
    assert parts == [2, 3]
    assert total == 4


def test_falls_back_to_section_path_without_raw_source(vault):
    (vault["raw"] / f"{DOC}.md").unlink()
    # END-boundary paths: Parts 2 and 3 end inside 2.1.
    assert _parts(f"§2.1 of [[{DOC}]]") == ([2, 3], 2)


def test_falls_back_when_parts_lack_char_spans(vault):
    for n, (start, end, path) in enumerate(_BOUNDS, start=1):
        _write_part(vault["folder"], n, start, end, path, spans=False)
    assert _parts(f"§2.1 of [[{DOC}]]") == ([2, 3], 2)


@pytest.mark.parametrize(
    "question",
    [
        f"§9.9 of [[{DOC}]]",  # no such section
        "§2.1 of a document nobody ingested",  # no identifiable document
        "這篇 JEPA 論文的主要貢獻是什麼？",  # no section marker
    ],
)
def test_returns_nothing_when_section_or_document_is_unknown(vault, question):
    assert _parts(question) == ([], 0)


def test_question_without_section_never_touches_the_filesystem(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("filesystem touched for an ordinary question")

    monkeypatch.setattr(section_locator, "_document_dirs", boom)
    assert locate_section_parts("What is a JEPA?", ["Some Doc"]) == ([], 0)


# ── default Q&A integration ───────────────────────────────────────────────


def _run_chat(monkeypatch, tmp_path, question, *, resolve=None):
    from watchers.prompt_watcher import PromptWatcher

    llm = MagicMock()
    llm.answer_query.return_value = "answer"
    llm.current_trace_ids.return_value = []
    llm.current_run_id.return_value = None
    rag = MagicMock()
    rag.query_similar_notes.return_value = ["RAG-SIMILAR-NOTE"]
    resolve = resolve or MagicMock(return_value=[])
    monkeypatch.setattr("services.command_dispatcher._resolve_source_paths", resolve)
    out = tmp_path / "from_llm"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr("services.command_dispatcher.FROM_LLM_DIR", out)

    prompt = tmp_path / "ask.md"
    prompt.write_text(question, encoding="utf-8")
    watcher = PromptWatcher(llm, rag)
    monkeypatch.setattr(watcher, "_archive_raw", MagicMock())
    watcher.process_prompt(prompt)
    _, context = llm.answer_query.call_args[0]
    return context, resolve


def test_chat_loads_located_parts_instead_of_the_whole_document(vault, monkeypatch, tmp_path):
    context, resolve = _run_chat(monkeypatch, tmp_path, f"[[{DOC}]] 的 §2.1 在說什麼？")

    assert [c.args[0] for c in resolve.call_args_list] == []  # whole doc not loaded
    for n in (2, 3, 4):
        assert f"PART-{n}-TEXT" in context
    assert "PART-7-TEXT" not in context
    assert "章節定位：§2.1" in context
    assert "source_start_char" not in context  # frontmatter stripped
    assert context.index("PART-2-TEXT") < context.index("PART-3-TEXT")
    assert "RAG-SIMILAR-NOTE" in context


def test_chat_keeps_explicit_part_links_and_says_when_capped(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(section_locator.settings, "SECTION_LOCATE_MAX_PARTS", 2)
    part_path = MagicMock()
    part_path.read_text.return_value = "EXPLICIT-PART-9"
    resolve = MagicMock(return_value=[(part_path, "direct")])

    context, resolve = _run_chat(
        monkeypatch,
        tmp_path,
        f"Section 2 of [[{DOC}]] and [[{DOC} (Part 9)]]",
        resolve=resolve,
    )

    assert [c.args[0] for c in resolve.call_args_list] == [f"{DOC} (Part 9)"]
    assert "EXPLICIT-PART-9" in context
    assert "共涵蓋 4 個 Part" in context and "只載入前 2 個" in context


def test_chat_still_answers_when_section_lookup_fails(vault, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("frontmatter exploded")

    monkeypatch.setattr("services.command_dispatcher.locate_section_parts", boom)
    doc_path = MagicMock()
    doc_path.read_text.return_value = "WHOLE-DOC-TEXT"
    context, resolve = _run_chat(
        monkeypatch,
        tmp_path,
        f"[[{DOC}]] 的 §2.1",
        resolve=MagicMock(return_value=[(doc_path, "stitched")]),
    )

    assert "WHOLE-DOC-TEXT" in context  # falls back to the plain link load
    assert "RAG-SIMILAR-NOTE" in context
