"""C2: CodeReviewAgent — map/reduce/report flow + wiring."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import agents.code_review_agent as cra_mod
from agents.code_review_agent import CodeReviewAgent
from services.command_dispatcher import detect_intent


def _packed_note(dir_, title, *, identifiers, body_files):
    dir_.mkdir(parents=True, exist_ok=True)
    ids = "\n".join(f"  - {i}" for i in identifiers)
    secs = "\n\n".join(f"## {p}\n\n```python\n{code}\n```" for p, code in body_files)
    (dir_ / f"{title}.md").write_text(
        f"---\ntype: packed-code\nidentifiers:\n{ids}\n---\n\n# Packed\n\n{secs}\n",
        encoding="utf-8",
    )


def _agent(llm):
    a = CodeReviewAgent.__new__(CodeReviewAgent)
    a.llm = llm
    a.stats = {"input_chars": 0, "output_chars": 0}
    a._write_report = lambda title, body, rtype, meta=None: (None, body)
    return a


class _LLM:
    def __init__(self, findings, report):
        self._findings = findings
        self._report = report
        self.aq_kwargs = None

    def _complete_json(self, **kw):
        return list(self._findings)

    def answer_query(self, **kw):
        self.aq_kwargs = kw
        return self._report


def test_full_flow_axes_and_identifier_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(cra_mod, "CODE_REVIEW_DIR", tmp_path)
    _packed_note(
        tmp_path,
        "mymod",
        identifiers=["RecallAgent"],
        body_files=[("mymod.py", "class RecallAgent: ...")],
    )
    llm = _LLM(
        findings=[
            {
                "anchor": "RecallAgent",
                "severity": "high",
                "category": "correctness",
                "claim": "c",
                "excerpt": "class RecallAgent",
                "suggestion": "s",
            }
        ],
        # leading frontmatter (stripped) + a mangled identifier in backticks (snapped)
        report="---\ntitle: x\n---\n## 總評\n`recallagent` 有問題。",
    )
    agent = _agent(llm)
    out = agent.execute({"user_directive": "@ling-code-review [[mymod]]"})

    # path-A three axes
    assert llm.aq_kwargs["persona"] == "coder"
    assert llm.aq_kwargs["operation"] == "review_code"
    assert llm.aq_kwargs["forced_template"] == "code-review-rpt"
    # leading frontmatter stripped, identifier snapped back
    assert "`RecallAgent`" in out
    assert "recallagent" not in out
    assert "title: x" not in out


def test_reduce_dedups_by_file_anchor_category(tmp_path, monkeypatch):
    monkeypatch.setattr(cra_mod, "CODE_REVIEW_DIR", tmp_path)
    _packed_note(tmp_path, "m", identifiers=[], body_files=[("m.py", "def f(): ...")])
    dup = {
        "anchor": "f",
        "severity": "low",
        "category": "readability",
        "claim": "c",
        "excerpt": "def f",
        "suggestion": "s",
    }
    llm = _LLM(findings=[dup, dict(dup)], report="## 總評\nok")
    agent = _agent(llm)
    agent.execute({"user_directive": "@ling-code-review [[m]]"})
    ctx = llm.aq_kwargs["wiki_context"]
    assert ctx.count("→ f（readability）") == 1  # two identical findings → one


def test_no_findings_skips_llm_report(tmp_path, monkeypatch):
    monkeypatch.setattr(cra_mod, "CODE_REVIEW_DIR", tmp_path)
    _packed_note(tmp_path, "clean", identifiers=[], body_files=[("clean.py", "def ok(): return 1")])
    llm = _LLM(findings=[], report="SHOULD NOT BE USED")
    agent = _agent(llm)
    out = agent.execute({"user_directive": "@ling-code-review [[clean]]"})
    assert llm.aq_kwargs is None  # answer_query never called
    assert "沒有發現值得提的問題" in out


def test_missing_note_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(cra_mod, "CODE_REVIEW_DIR", tmp_path)
    llm = _LLM(findings=[], report="")
    agent = _agent(llm)
    out = agent.execute({"user_directive": "@ling-code-review [[nope]]"})
    assert "make pack-code" in out  # guides the user to pack first


def test_chunker_ignores_hash_heading_inside_fence():
    # REGRESSION (review fix): a top-column `## comment` inside the fence used
    # to shear the section in two (fence-unaware `^## ` split).
    body = "## real/file.py\n\n```python\nx = 1\n## top-column comment\ny = 2\n```\n"
    chunks = CodeReviewAgent._chunk_by_file(body)  # fallback fence-tracking mode
    assert len(chunks) == 1
    assert chunks[0][0] == "real/file.py"
    assert "## top-column comment" in chunks[0][1]  # kept as code, not a boundary


def test_chunker_whitelist_mode_survives_fenced_backticks():
    # Whitelist mode: even a stray top-column ``` inside the code (e.g. a
    # docstring holding a fenced example) cannot confuse the boundaries,
    # because only headings matching source_paths split.
    body = (
        "## a.py\n\n```python\ns = '''\n```\n'''\n## not a real heading\n```\n\n"
        "## b.py\n\n```python\ny = 2\n```\n"
    )
    chunks = CodeReviewAgent._chunk_by_file(body, ["a.py", "b.py"])
    assert [c[0] for c in chunks] == ["a.py", "b.py"]
    assert "## not a real heading" in chunks[0][1]


def test_routing_code_review_vs_review():
    # substring routing: the longer trigger must win, and review must be unaffected
    assert detect_intent("@ling-code-review", "@ling-code-review") == "code-review"
    assert detect_intent("@ling-review", "@ling-review") == "review"
    assert detect_intent("x", "/code-review [[m]]") == "code-review"
