"""C2: pack_code CLI — deterministic repo→vault code bundling."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from tools import pack_code


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo root with a CodeReview/ output dir, wired into pack_code."""
    monkeypatch.setattr(pack_code, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pack_code, "CODE_REVIEW_DIR", tmp_path / "CodeReview")
    return tmp_path


def test_packs_file_with_identifiers_and_metadata(repo):
    (repo / "mod.py").write_text(
        "def alpha():\n    return 1\n\nclass Beta:\n    def gamma(self):\n        return 2\n",
        encoding="utf-8",
    )
    out = pack_code.pack(["mod.py"], None)
    text = out.read_text(encoding="utf-8")
    assert out.name == "mod.md"
    assert "type: packed-code" in text
    assert "file_count: 1" in text
    assert "- mod.py" in text  # source_paths
    for ident in ("alpha", "Beta", "gamma"):
        assert f"  - {ident}" in text  # identifiers manifest
    assert "```python" in text and "def alpha():" in text  # verbatim fenced code


def test_directory_recurses_py_only(repo):
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (pkg / "notes.txt").write_text("ignore me", encoding="utf-8")
    out = pack_code.pack(["pkg"], "bundle")
    text = out.read_text(encoding="utf-8")
    assert "- pkg/a.py" in text
    assert "notes.txt" not in text


def test_rejects_oversized_file(repo):
    (repo / "big.py").write_text("x = 1\n" + "# pad\n" * 40000, encoding="utf-8")
    with pytest.raises(SystemExit):
        pack_code.pack(["big.py"], None)


def test_refuses_path_outside_repo(repo, tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        pack_code.pack([str(outside)], None)
