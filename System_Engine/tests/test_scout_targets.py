"""services/scout/targets.py — targets-file parsing and forgiving validation.

Primary format is a Markdown table in the body (Obsidian's properties UI
can't edit nested frontmatter lists); the legacy frontmatter `targets:` list
still parses.
"""

from services.scout.targets import load_targets

TABLE = """---
language: English
---

# 🔭 Scout

| url | parser | cadence | max_items |
| --- | --- | --- | --- |
| https://github.com/trending | github_trending | daily | 10 |
| <https://news.ycombinator.com/newest> |  |  |  |
| [某站](https://blog.example.org/feed.xml) | feed | weekly | 5 |
|  | feed | daily | 3 |

其他筆記文字不受影響。
"""


def test_load_targets_from_markdown_table(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text(TABLE, encoding="utf-8")
    targets, language = load_targets(path)
    assert language == "English"
    # Row with an empty url cell is dropped; the other three parse.
    assert [t.url for t in targets] == [
        "https://github.com/trending",
        "https://news.ycombinator.com/newest",  # <angle brackets> stripped
        "https://blog.example.org/feed.xml",  # [markdown](link) unwrapped
    ]
    assert targets[0].parser == "github_trending" and targets[0].max_items == 10
    # Empty cells → defaults.
    assert targets[1].parser is None
    assert targets[1].cadence == "daily"
    assert targets[1].max_items is None
    assert targets[2].cadence == "weekly" and targets[2].max_items == 5


def test_table_and_legacy_frontmatter_merge(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text(
        """---
targets:
  - url: https://arxiv.org/list/cs.AI/recent
---

| url |
| --- |
| https://github.com/trending |
""",
        encoding="utf-8",
    )
    targets, _ = load_targets(path)
    assert [t.url for t in targets] == [
        "https://arxiv.org/list/cs.AI/recent",  # frontmatter first
        "https://github.com/trending",
    ]


def test_non_target_tables_are_ignored(tmp_path):
    path = tmp_path / "Scout.md"
    path.write_text(
        """# notes

| name | note |
| --- | --- |
| foo | bar |

| url | cadence |
| --- | --- |
| https://github.com/trending | daily |
""",
        encoding="utf-8",
    )
    targets, _ = load_targets(path)
    assert [t.url for t in targets] == ["https://github.com/trending"]


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
