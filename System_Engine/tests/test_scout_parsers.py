"""services/scout/parsers — fixture-driven extraction + URL auto-detection."""

import json
import types

import pytest

from services.scout.models import ScoutParserError, ScoutTarget
from services.scout.parsers import arxiv, feed, github_trending, hackernews, resolve_parser

GH_TRENDING_HTML = """
<html><body>
<article class="Box-row">
  <h2><a href="/octo/rocket">octo / rocket</a></h2>
  <p> A fast rocket   framework. </p>
  <span itemprop="programmingLanguage">Rust</span>
  <a href="/octo/rocket/stargazers"> 12,345 </a>
</article>
<article class="Box-row">
  <h2><a href="/ling/ling">ling / ling</a></h2>
</article>
</body></html>
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
        {
            "title": "Ask HN: No external url",
            "url": None,
            "objectID": "102",
            "points": 3,
            "num_comments": 1,
        },
        {"title": "", "objectID": "103"},  # unparseable → dropped
    ]
}


class FakeClient:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self.payload = payload
        self.calls = []

    def get(self, url, *, source, **kwargs):
        self.calls.append((url, source))
        return types.SimpleNamespace(text=self.text, json=lambda: self.payload)


def test_github_trending_parses_rows():
    client = FakeClient(text=GH_TRENDING_HTML)
    items = github_trending.fetch(client, ScoutTarget(url="https://github.com/trending"))
    assert [i.title for i in items] == ["octo/rocket", "ling/ling"]
    assert items[0].url == "https://github.com/octo/rocket"
    assert items[0].dedupe_key == items[0].url
    assert items[0].snippet == "A fast rocket framework."
    assert "Rust" in items[0].stats and "⭐12,345" in items[0].stats
    assert client.calls[0][1] == github_trending.SOURCE


def test_github_trending_layout_change_raises():
    client = FakeClient(text="<html><body><div>redesigned!</div></body></html>")
    with pytest.raises(ScoutParserError):
        github_trending.fetch(client, ScoutTarget(url="https://github.com/trending"))


def test_hackernews_uses_algolia_api_and_falls_back_to_item_page():
    client = FakeClient(payload=HN_HITS)
    target = ScoutTarget(url="https://news.ycombinator.com/newest", max_items=15)
    items = hackernews.fetch(client, target)
    assert "hn.algolia.com" in client.calls[0][0]
    assert "hitsPerPage=15" in client.calls[0][0]
    assert len(items) == 2  # the title-less hit is dropped
    assert items[0].url == "https://example.com/thing"
    # Story identity (not the external url) is the dedupe key.
    assert items[0].dedupe_key == "https://news.ycombinator.com/item?id=101"
    # Ask HN with no url links to the discussion page.
    assert items[1].url == "https://news.ycombinator.com/item?id=102"


def test_hackernews_bad_payload_raises():
    client = FakeClient(payload={"hits": "nope"})
    with pytest.raises(ScoutParserError):
        hackernews.fetch(client, ScoutTarget(url="https://news.ycombinator.com/newest"))

    class Boom:
        def get(self, url, *, source, **kwargs):
            def _raise():
                raise ValueError(json.dumps({"err": 1}))

            return types.SimpleNamespace(json=_raise)

    with pytest.raises(ScoutParserError):
        hackernews.fetch(Boom(), ScoutTarget(url="https://news.ycombinator.com/newest"))


ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2507.01234v2</id>
    <title>A  Neat
 Paper</title>
    <summary>We study things.
 Deeply.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
  </entry>
  <entry>
    <id></id>
    <title>No id, dropped</title>
  </entry>
</feed>
"""


def test_arxiv_list_url_goes_through_official_api():
    client = FakeClient(text=ARXIV_ATOM)
    target = ScoutTarget(url="https://arxiv.org/list/cs.AI/recent", max_items=10)
    items = arxiv.fetch(client, target)
    url, source = client.calls[0]
    assert "export.arxiv.org/api/query" in url
    assert "cat:cs.AI" in url and "max_results=10" in url
    assert source == arxiv.SOURCE

    assert len(items) == 1
    item = items[0]
    assert item.title == "A Neat Paper"  # whitespace squashed
    assert item.url == "http://arxiv.org/abs/2507.01234v2"
    assert item.dedupe_key == "http://arxiv.org/abs/2507.01234"  # version stripped
    assert item.content == "We study things. Deeply."  # abstract IS the content
    assert "Ada Lovelace" in item.stats


def test_arxiv_rejects_non_list_urls_and_bad_xml():
    with pytest.raises(ScoutParserError):
        arxiv.fetch(FakeClient(text=ARXIV_ATOM), ScoutTarget(url="https://arxiv.org/abs/1"))
    with pytest.raises(ScoutParserError):
        arxiv.fetch(
            FakeClient(text="<html>oops"),
            ScoutTarget(url="https://arxiv.org/list/cs.AI/recent"),
        )


def test_arxiv_section_title_includes_category():
    assert (
        arxiv.section_title(ScoutTarget(url="https://arxiv.org/list/cs.AI/recent"))
        == "arXiv cs.AI (recent)"
    )


RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Some Blog</title>
  <item>
    <title>Post &amp; One</title>
    <link>https://blog.example.org/post-1</link>
    <guid>tag:blog,post-1</guid>
    <description><![CDATA[<p>Body   <b>text</b> here.</p>]]></description>
  </item>
  <item><title>No link, dropped</title></item>
</channel></rss>
"""

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:entry-2</id>
    <title>Atom Entry</title>
    <link rel="alternate" href="https://blog.example.org/post-2"/>
    <summary>Short summary.</summary>
  </entry>
</feed>
"""


class SequencedClient:
    """Returns queued responses in order (autodiscovery = 2 fetches)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, source, **kwargs):
        self.calls.append((url, source))
        return self.responses.pop(0)


def _resp(text, content_type="text/html"):
    return types.SimpleNamespace(text=text, headers={"Content-Type": content_type})


def test_feed_parses_rss_directly():
    client = SequencedClient([_resp(RSS_XML, "application/rss+xml")])
    items = feed.fetch(client, ScoutTarget(url="https://blog.example.org/feed.xml"))
    assert len(items) == 1  # link-less entry dropped
    assert items[0].title == "Post & One"
    assert items[0].url == "https://blog.example.org/post-1"
    assert items[0].dedupe_key == "tag:blog,post-1"  # guid wins over link
    assert items[0].snippet == "Body text here."  # embedded HTML flattened
    assert client.calls[0][1] == "blog.example.org"  # throttled per host


def test_feed_autodiscovers_from_html_page():
    html = (
        '<html><head><link rel="alternate" type="application/atom+xml" '
        'href="/feed.atom"></head><body>hi</body></html>'
    )
    client = SequencedClient([_resp(html), _resp(ATOM_XML, "application/atom+xml")])
    items = feed.fetch(client, ScoutTarget(url="https://blog.example.org/"))
    assert client.calls[1][0] == "https://blog.example.org/feed.atom"  # resolved relative href
    assert items[0].title == "Atom Entry"
    assert items[0].url == "https://blog.example.org/post-2"
    assert items[0].dedupe_key == "urn:entry-2"


def test_feed_page_without_feed_fails_visibly():
    client = SequencedClient([_resp("<html><body>no feed here</body></html>")])
    with pytest.raises(ScoutParserError):
        feed.fetch(client, ScoutTarget(url="https://blog.example.org/"))


def test_resolve_parser_explicit_name_wins():
    target = ScoutTarget(url="https://example.com/", parser="hackernews")
    assert resolve_parser(target) is hackernews


def test_resolve_parser_auto_detects_by_url():
    assert (
        resolve_parser(ScoutTarget(url="https://github.com/trending/python?since=weekly"))
        is github_trending
    )
    assert resolve_parser(ScoutTarget(url="https://news.ycombinator.com/newest")) is hackernews
    assert resolve_parser(ScoutTarget(url="https://arxiv.org/list/cs.AI/recent")) is arxiv
    # P2.1: everything unmatched falls back to the generic feed parser…
    assert resolve_parser(ScoutTarget(url="https://arxiv.org/abs/2507.01234")) is feed
    assert resolve_parser(ScoutTarget(url="https://github.com/octo/rocket")) is feed
    assert resolve_parser(ScoutTarget(url="https://example.com/blog")) is feed
    # …but a typo'd EXPLICIT parser name still fails loudly.
    assert resolve_parser(ScoutTarget(url="https://example.com/", parser="typo")) is None
