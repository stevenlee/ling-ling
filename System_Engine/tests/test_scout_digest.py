"""services/scout/digest.py — orchestration: cadence gate, dedupe, failure
isolation, content-grounded per-item analysis, LLM-degradation fallbacks."""

import types
from datetime import datetime, timedelta

import pytest
import requests

from core.parsing.markdown_metadata import parse_markdown_metadata
from services.scout.digest import run_scout_digest
from services.scout.state import ScoutState

# NOTE: FakeClient serves ARTICLE_HTML for every non-listing URL, so per-item
# content fetches succeed in all tests without extra wiring.

NOW = datetime(2026, 7, 11, 8, 0, 0)

GH_HTML = """
<article class="Box-row">
  <h2><a href="/octo/rocket">octo/rocket</a></h2>
  <p>A fast rocket framework.</p>
  <a href="/octo/rocket/stargazers">12,345</a>
</article>
"""

HN_HITS = {
    "hits": [
        {
            "title": "Show HN: A thing",
            "url": "https://example.com/thing",
            "objectID": "101",
            "points": 42,
            "num_comments": 7,
        },
    ]
}

ARTICLE_HTML = "<html><body><article><p>Article body text.</p></article></body></html>"

TARGETS_MD = """---
targets:
  - url: https://github.com/trending
  - url: https://news.ycombinator.com/newest
---
"""


def _response(text="", payload=None):
    return types.SimpleNamespace(
        text=text, json=lambda: payload, headers={"Content-Type": "text/html"}
    )


class FakeClient:
    """Dispatches by URL; a url listed in ``broken`` raises a network error."""

    def __init__(self, broken=()):
        self.broken = broken
        self.calls = []

    def get(self, url, *, source, **kwargs):
        self.calls.append(url)
        if any(b in url for b in self.broken):
            raise requests.exceptions.ConnectionError("boom")
        if "github.com/trending" in url:
            return _response(text=GH_HTML)
        if "hn.algolia.com" in url:
            return _response(payload=HN_HITS)
        return _response(text=ARTICLE_HTML)  # per-item content fetches


class FakeLLM:
    def __init__(self, summarize_reply="這是逐項概要。"):
        self.summarize_reply = summarize_reply
        self.calls = []  # (stage, user_msg)

    def complete(self, system_prompt, user_msg, *, stage="complete", **kwargs):
        self.calls.append((stage, user_msg))
        return self.summarize_reply if stage == "scout_summarize" else ""

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


class FakeRAG:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.queries = []

    def query_notes(self, query_text, top_k=3, **kwargs):
        self.queries.append(query_text)
        return self.hits


def _run(tmp_path, *, llm=None, client=None, rag=None, now=NOW, targets_md=TARGETS_MD):
    targets_file = tmp_path / "Scout.md"
    if not targets_file.exists():
        targets_file.write_text(targets_md, encoding="utf-8")
    return run_scout_digest(
        llm or FakeLLM(),
        rag=rag,
        targets_file=targets_file,
        state_file=tmp_path / "scout_state.json",
        report_dir=tmp_path / "out",
        mirror_dir=tmp_path / "mirror",
        client=client or FakeClient(),
        now=now,
    )


def test_happy_path_writes_report(tmp_path):
    llm = FakeLLM()
    result = _run(tmp_path, llm=llm)
    assert result.status == "succeeded"
    assert result.report_path is not None
    assert result.report_path.name == "✅Scout-2026-07-11.md"

    content = result.report_path.read_text(encoding="utf-8")
    metadata = parse_markdown_metadata(content)
    assert metadata["type"] == "Scout"
    assert metadata["new_items"] == 2
    assert metadata["targets_failed"] == []
    assert "## GitHub Trending" in content
    assert "[octo/rocket](https://github.com/octo/rocket)" in content
    assert "這是逐項概要" in content  # LLM summary attached to the item line
    assert "## Hacker News (newest)" in content
    # R2: no cross-source analysis section (returns in Phase 2, grounded).
    assert "綜合分析" not in content
    # Exactly one summarize call per ITEM (2 items) — nothing else.
    assert llm.stages == ["scout_summarize", "scout_summarize"]


def test_item_analysis_is_grounded_in_fetched_content(tmp_path):
    llm = FakeLLM()
    client = FakeClient()
    _run(tmp_path, llm=llm, client=client)
    # The item pages themselves were fetched...
    assert any("example.com/thing" in url for url in client.calls)
    assert any("github.com/octo/rocket" in url for url in client.calls)
    # ...and their extracted text reached the per-item LLM prompt.
    summarize_msgs = [msg for stage, msg in llm.calls if stage == "scout_summarize"]
    assert all("Article body text." in msg for msg in summarize_msgs)


def test_dead_item_link_degrades_to_snippet_grounding(tmp_path):
    # Only the ITEM page is broken (the HN listing itself works): the summary
    # call still happens, grounded on title+snippet instead of content, and
    # the report line says so.
    llm = FakeLLM()
    result = _run(tmp_path, llm=llm, client=FakeClient(broken=("example.com",)))
    assert result.status == "succeeded"
    hn_msgs = [msg for stage, msg in llm.calls if "Show HN" in msg]
    assert hn_msgs and "(unavailable" in hn_msgs[0]
    report = result.report_path.read_text(encoding="utf-8")
    assert "（未取得內文，僅依標題與摘錄分析）" in report
    # The domain strike was persisted for the adaptive skiplist.
    assert ScoutState(tmp_path / "scout_state.json").is_seen(
        "https://news.ycombinator.com/item?id=101"
    )


def test_blocked_domain_skips_content_fetch_entirely(tmp_path):
    state = ScoutState(tmp_path / "scout_state.json")
    for _ in range(3):
        state.record_content_fetch("example.com", ok=False, now=NOW - timedelta(days=1))
    state.save()

    client = FakeClient()
    result = _run(tmp_path, client=client)
    # The HN item links to example.com — content fetch must not even be tried.
    assert not any("example.com/thing" in url for url in client.calls)
    report = result.report_path.read_text(encoding="utf-8")
    assert "（未取得內文，僅依標題與摘錄分析）" in report


def test_report_is_mirrored_byte_identical(tmp_path):
    result = _run(tmp_path)
    mirror = tmp_path / "mirror" / result.report_path.name
    assert mirror.exists()
    assert mirror.read_bytes() == result.report_path.read_bytes()


def test_streak_line_for_items_riding_the_list(tmp_path):
    # octo/rocket was on trending the two previous days → today is day 3.
    state = ScoutState(tmp_path / "scout_state.json")
    state.record_sighting(
        "https://github.com/octo/rocket", title="octo/rocket", now=NOW - timedelta(days=2)
    )
    state.record_sighting(
        "https://github.com/octo/rocket", title="octo/rocket", now=NOW - timedelta(days=1)
    )
    state.save()

    result = _run(tmp_path)  # HN item is still new → report gets written
    content = result.report_path.read_text(encoding="utf-8")
    assert "🔁 持續上榜" in content
    assert "[octo/rocket](https://github.com/octo/rocket)（第 3 天）" in content
    # The riding item is NOT re-listed as a fresh entry.
    metadata = parse_markdown_metadata(content)
    assert metadata["new_items"] == 1


def test_bridging_links_related_vault_notes(tmp_path):
    rag = FakeRAG(
        hits=[
            {"metadata": {"title": "我的量子筆記"}, "distance": 0.30},
            {"metadata": {"title": "太遠的筆記"}, "distance": 0.90},
            # Mirrored reports carry the ✅ prefix in their RAG title — the
            # first live dig linked [[✅Scout-2026-07-11]] back to itself.
            {"metadata": {"title": "✅Scout-2026-07-10"}, "distance": 0.10},
            {"metadata": {"title": "✅Dig-Something"}, "distance": 0.10},
        ]
    )
    result = _run(tmp_path, rag=rag)
    content = result.report_path.read_text(encoding="utf-8")
    assert "相關筆記: [[我的量子筆記]]" in content
    assert "太遠的筆記" not in content  # over the distance gate
    assert "Scout-2026-07-10" not in content  # own mirrored reports excluded
    assert "Dig-Something" not in content
    assert rag.queries  # one query per item


def test_no_rag_means_no_bridging(tmp_path):
    result = _run(tmp_path, rag=None)
    content = result.report_path.read_text(encoding="utf-8")
    assert "相關筆記" not in content


def test_second_run_dedupes_and_skips_report(tmp_path):
    first = _run(tmp_path)
    assert first.report_path is not None
    second = _run(tmp_path, now=NOW + timedelta(days=1))
    assert second.status == "succeeded"
    assert second.report_path is None
    assert "No new items" in second.summary


def test_target_failure_is_isolated(tmp_path):
    result = _run(tmp_path, client=FakeClient(broken=("github",)))
    assert result.status == "succeeded"
    content = result.report_path.read_text(encoding="utf-8")
    metadata = parse_markdown_metadata(content)
    assert len(metadata["targets_failed"]) == 1
    assert "github.com/trending" in metadata["targets_failed"][0]
    # HN still made it into the report; the failure shows in 抓取狀況.
    assert "## Hacker News (newest)" in content
    assert "抓取失敗" in content


def test_weekly_cadence_gate(tmp_path):
    targets_md = """---
targets:
  - url: https://github.com/trending
    cadence: weekly
---
"""
    state = ScoutState(tmp_path / "scout_state.json")
    state.mark_crawled("https://github.com/trending", now=NOW - timedelta(days=2))
    state.save()
    client = FakeClient()
    result = _run(tmp_path, client=client, targets_md=targets_md)
    assert "No new items" in result.summary
    assert client.calls == []  # gate fired before any HTTP

    # 7 days later the same target is due again.
    result = _run(tmp_path, client=client, now=NOW + timedelta(days=7), targets_md=targets_md)
    assert result.report_path is not None


def test_blank_llm_summary_falls_back_to_snippet(tmp_path):
    llm = FakeLLM(summarize_reply="")
    result = _run(tmp_path, llm=llm)
    content = result.report_path.read_text(encoding="utf-8")
    # Item line falls back to the crawled snippet instead of vanishing.
    assert "A fast rocket framework." in content


def test_interrupted_run_never_swallows_items(tmp_path):
    # R5: seen-marks persist only AFTER the report is on disk. If the run dies
    # before that (daemon restarts often), the next run must re-see everything.
    (tmp_path / "Scout.md").write_text(TARGETS_MD, encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the report dir should go", encoding="utf-8")

    with pytest.raises(OSError):
        run_scout_digest(
            FakeLLM(),
            targets_file=tmp_path / "Scout.md",
            state_file=tmp_path / "scout_state.json",
            report_dir=blocker / "out",  # mkdir under a file → boom, like a kill
            mirror_dir=tmp_path / "mirror",
            client=FakeClient(),
            now=NOW,
        )
    # Nothing was persisted — a rerun starts clean and re-reports the items.
    assert not (tmp_path / "scout_state.json").exists()
    rerun = _run(tmp_path)
    assert rerun.report_path is not None
    assert parse_markdown_metadata(rerun.report_path.read_text(encoding="utf-8"))["new_items"] == 2


def test_no_targets_file(tmp_path):
    result = run_scout_digest(
        FakeLLM(),
        targets_file=tmp_path / "missing.md",
        state_file=tmp_path / "s.json",
        report_dir=tmp_path / "out",
        client=FakeClient(),
        now=NOW,
    )
    assert result.status == "skipped"
