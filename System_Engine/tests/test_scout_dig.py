"""services/scout/dig.py + DigAgent — @ling-dig deep-dive flow."""

import types

import requests

from agents.dig_agent import DigAgent
from services.command_dispatcher import detect_intent
from services.scout.dig import (
    DigResult,
    extract_links,
    first_url,
    run_dig,
)

MAIN_HTML = """
<html><head><title>  Interesting   Project </title></head><body>
<nav><a href="/login">Login</a></nav>
<article>
<p>A tool that does things, described at length here.</p>
<a href="/docs/guide">Read the full documentation</a>
<a href="https://arxiv.org/abs/2507.9">The underlying paper</a>
<a href="https://example.com/page#section">Fragment link</a>
<a href="https://example.com/x">ad</a>
</article>
</body></html>
"""

DOC_HTML = "<html><body><article><p>Deep documentation body text.</p></article></body></html>"


class FakeClient:
    def __init__(self, broken=()):
        self.broken = broken
        self.calls = []

    def get(self, url, *, source, headers=None, **kwargs):
        self.calls.append((url, headers or {}))
        if any(b in url for b in self.broken):
            raise requests.exceptions.ConnectionError("boom")
        text = MAIN_HTML if url == "https://example.com/page" else DOC_HTML
        return types.SimpleNamespace(text=text, headers={"Content-Type": "text/html"})


class FakeLLM:
    def __init__(self, select_reply="1, 2", synth_reply="## 深掘內容\n很深的分析。"):
        self.select_reply = select_reply
        self.synth_reply = synth_reply
        self.calls = []

    def complete(self, system_prompt, user_msg, *, stage="complete", **kwargs):
        self.calls.append((stage, user_msg))
        self.systems = getattr(self, "systems", []) + [system_prompt]
        return self.select_reply if stage == "dig_select" else self.synth_reply


def test_first_url():
    assert first_url("@ling-dig https://a.io/x, 謝謝") == "https://a.io/x"
    assert first_url("看看這個（https://a.io/y）") == "https://a.io/y"
    assert first_url("@ling-dig 沒有網址") is None


def test_extract_links_filters_chrome():
    links = extract_links(MAIN_HTML, base_url="https://example.com/page")
    urls = [link.url for link in links]
    assert "https://example.com/docs/guide" in urls  # relative resolved
    assert "https://arxiv.org/abs/2507.9" in urls
    assert "https://example.com/x" not in urls  # anchor text too short ("ad")
    assert not any("#" in u for u in urls)  # fragments stripped
    assert "https://example.com/page" not in urls  # self dropped
    assert not any("/login" in u for u in urls)  # chrome URLs pre-filtered


def test_run_dig_happy_path():
    llm = FakeLLM()
    result = run_dig(
        llm, "https://example.com/page", language="Traditional Chinese", client=FakeClient()
    )
    assert result.status == "succeeded"
    assert result.title == "Interesting Project"  # whitespace squashed
    assert "很深的分析" in result.body
    assert len(result.followed) == 2
    assert all(s.content for s in result.followed)
    # Selection prompt saw the numbered candidates; synthesis saw followed text.
    select_msg = next(msg for stage, msg in llm.calls if stage == "dig_select")
    assert "Read the full documentation" in select_msg
    synth_msg = next(msg for stage, msg in llm.calls if stage == "dig_synthesize")
    assert "Deep documentation body text." in synth_msg


def test_run_dig_select_none_and_linked_failure():
    llm = FakeLLM(select_reply="NONE")
    result = run_dig(llm, "https://example.com/page", language="English", client=FakeClient())
    assert result.status == "succeeded" and result.followed == []
    # No followed links → the no-links prompt variant (no "followed links add"
    # section that the LLM would otherwise fill with boilerplate).
    assert "ONE page's full text" in llm.systems[-1]

    # Candidates (login pre-filtered): 1=docs, 2=arxiv.
    llm = FakeLLM(select_reply="2")  # arxiv link — made unreachable
    result = run_dig(
        llm, "https://example.com/page", language="English", client=FakeClient(broken=("arxiv",))
    )
    assert result.status == "succeeded"  # linked failure never kills the dig
    assert result.followed[0].error


def test_run_dig_main_fetch_failure():
    # broken=example.com also matches the Wayback URL (it embeds the original)
    # → both direct and snapshot fail → failed, with both causes in the message.
    result = run_dig(
        FakeLLM(),
        "https://example.com/page",
        language="English",
        client=FakeClient(broken=("example.com",)),
    )
    assert result.status == "failed"
    assert "Wayback" in result.summary


def test_run_dig_falls_back_to_wayback_snapshot():
    class WalledClient:
        """Direct fetch 403s (axios/WaPo class); the archive has a copy."""

        def __init__(self):
            self.calls = []

        def get(self, url, *, source, headers=None, **kwargs):
            self.calls.append(url)
            if url.startswith("https://web.archive.org/"):
                return types.SimpleNamespace(text=MAIN_HTML, headers={"Content-Type": "text/html"})
            raise requests.exceptions.HTTPError("403 Client Error: Forbidden")

    llm = FakeLLM()
    result = run_dig(llm, "https://example.com/page", language="English", client=WalledClient())
    assert result.status == "succeeded"
    assert result.via == "wayback"
    assert "Wayback" in result.summary
    assert result.followed == []  # snapshot links are archive-prefixed → not followed
    # Only the synthesize call ran (no link selection without candidates).
    assert [stage for stage, _ in llm.calls] == ["dig_synthesize"]


def test_reddit_url_normalized_to_old_reddit():
    from services.scout.content import normalize_fetch_url

    assert (
        normalize_fetch_url("https://www.reddit.com/r/x/comments/1/y/?share_id=z")
        == "https://old.reddit.com/r/x/comments/1/y/?share_id=z"
    )
    assert normalize_fetch_url("https://example.com/a") == "https://example.com/a"

    client = FakeClient()
    run_dig(
        FakeLLM(select_reply="NONE"),
        "https://www.reddit.com/r/x/comments/1/y/",
        language="English",
        client=client,
    )
    assert client.calls[0][0].startswith("https://old.reddit.com/")


def test_intent_route():
    assert detect_intent("note @ling-dig.md", "") == "dig"
    assert detect_intent("x.md", "please /dig https://a.io") == "dig"


def test_dig_agent_composes_and_writes(monkeypatch, tmp_path):
    import agents.dig_agent as agent_mod
    import services.scout.dig as dig_mod
    from core.config import settings

    monkeypatch.setattr(settings, "SCOUT_MIRROR", False, raising=False)
    monkeypatch.setattr(
        dig_mod,
        "run_dig",
        lambda llm, url, **kw: DigResult("succeeded", "ok", title="T", body="## 分析\n內容。"),
    )

    written = {}

    def fake_write(self, title, body, report_type, metadata=None):
        written.update(title=title, body=body, report_type=report_type, metadata=metadata)
        return tmp_path / "r.md", body

    monkeypatch.setattr(agent_mod.DigAgent, "_write_report", fake_write)
    agent = DigAgent(llm=object(), rag=None)
    output = agent.execute({"user_directive": "@ling-dig https://a.io/x"})
    assert written["report_type"] == "Dig"
    assert written["metadata"] == {"source_url": "https://a.io/x"}
    assert "🔍 Scout 深掘：T" in output and "內容。" in output


def test_dig_agent_without_url_errors(monkeypatch):
    agent = DigAgent(llm=object(), rag=None)
    monkeypatch.setattr(agent, "_error_report", lambda msg: f"ERR:{msg}")
    output = agent.execute({"user_directive": "@ling-dig 幫我挖"})
    assert output.startswith("ERR:")
