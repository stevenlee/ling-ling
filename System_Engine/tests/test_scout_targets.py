"""services/scout/targets.py — targets-file parsing and forgiving validation."""

from services.scout.targets import load_targets

VALID = """---
targets:
  - url: https://github.com/trending
    parser: github_trending
    cadence: daily
    max_items: 10
  - url: https://news.ycombinator.com/newest
language: English
---

Body notes are ignored.
"""


def test_load_valid_targets(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text(VALID, encoding="utf-8")
    targets, language = load_targets(path)
    assert language == "English"
    assert [t.url for t in targets] == [
        "https://github.com/trending",
        "https://news.ycombinator.com/newest",
    ]
    assert targets[0].parser == "github_trending"
    assert targets[0].max_items == 10
    # Omitted fields fall back to defaults.
    assert targets[1].parser is None
    assert targets[1].cadence == "daily"
    assert targets[1].max_items is None


def test_missing_file_returns_empty(tmp_path):
    targets, language = load_targets(tmp_path / "nope.md")
    assert targets == [] and language == ""


def test_broken_entries_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text(
        """---
targets:
  - url: not-a-url
  - just-a-string
  - url: https://news.ycombinator.com/newest
    cadence: fortnightly
    max_items: banana
---
""",
        encoding="utf-8",
    )
    targets, _ = load_targets(path)
    # Only the entry with a valid url survives; typo'd cadence and bad
    # max_items degrade to defaults instead of killing it.
    assert len(targets) == 1
    assert targets[0].cadence == "daily"
    assert targets[0].max_items is None


def test_no_targets_key(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text("---\nlanguage: English\n---\n", encoding="utf-8")
    targets, language = load_targets(path)
    assert targets == [] and language == "English"
