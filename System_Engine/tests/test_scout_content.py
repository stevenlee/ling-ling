"""services/scout/content.py — best-effort item-content extraction."""

import types

import pytest
import requests

from services.scout.content import MAX_CONTENT_CHARS, extract_text, fetch_item_content
from services.scout.models import CrawledItem


NEWS_BODY = (
    "Economic activity expanded in the third quarter as household spending and business "
    "investment recovered. The national statistics office reported that output increased "
    "while inflation moderated. Analysts cautioned that financing costs remain high and "
    "that regional growth is uneven. The central bank will review employment, wages and "
    "consumer prices before its next policy meeting. Manufacturers reported stronger "
    "orders, although exporters continue to face uncertainty about demand overseas."
)


def _item(url="https://example.com/post", content=""):
    return CrawledItem(title="t", url=url, dedupe_key=url, content=content)


class FakeClient:
    def __init__(self, *, text="", content_type="text/html; charset=utf-8", error=None):
        self.text = text
        self.content_type = content_type
        self.error = error
        self.calls = []

    def get(self, url, *, source, **kwargs):
        self.calls.append((url, source))
        if self.error:
            raise self.error
        return types.SimpleNamespace(text=self.text, headers={"Content-Type": self.content_type})


def test_extract_text_strips_boilerplate_and_keeps_article():
    html = """
    <html><head><style>.x{}</style><script>evil()</script></head>
    <body><nav>menu menu</nav>
    <article><h1>Title</h1><p>First   paragraph.</p><p>Second.</p></article>
    <footer>© nobody</footer></body></html>
    """
    text = extract_text(html)
    assert "First paragraph." in text and "Second." in text
    assert "menu" not in text and "evil" not in text and "©" not in text


def test_extract_text_is_bounded():
    html = "<html><body><p>" + "x" * (MAX_CONTENT_CHARS * 2) + "</p></body></html>"
    assert len(extract_text(html)) == MAX_CONTENT_CHARS


def test_fetch_throttles_by_item_domain():
    client = FakeClient(text="<p>hi</p>")
    fetch_item_content(client, _item("https://blog.example.org/a"))
    assert client.calls[0][1] == "blog.example.org"


def test_fetch_uses_browser_profile_headers():
    # News-site WAFs 403 unknown bot UAs — article fetches present a browser
    # profile (listing/API fetches keep the honest Scout UA).
    captured = {}

    class Client:
        def get(self, url, *, source, headers=None, **kwargs):
            captured["headers"] = headers
            return types.SimpleNamespace(text="<p>hi</p>", headers={"Content-Type": "text/html"})

    fetch_item_content(Client(), _item())
    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]
    assert "Accept-Language" in captured["headers"]


def test_prefilled_content_skips_fetch():
    client = FakeClient()
    assert fetch_item_content(client, _item(content="already here")) == "already here"
    assert client.calls == []


def test_network_error_and_non_html_return_empty():
    boom = FakeClient(error=requests.exceptions.ConnectionError("boom"))
    assert fetch_item_content(boom, _item()) == ""
    pdf = FakeClient(text="%PDF-1.7", content_type="application/pdf")
    assert fetch_item_content(pdf, _item()) == ""


def test_google_news_resolves_before_fetching_body():
    import json

    wrapper = "https://news.google.com/rss/articles/CBMiabc?oc=5"
    publisher = "https://publisher.example/economy"

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            text = (
                '<div data-n-a-sg="signature" data-n-a-ts="123"></div>'
                if url == wrapper
                else f"<article><p>{NEWS_BODY}</p></article>"
            )
            return types.SimpleNamespace(url=url, text=text, headers={"Content-Type": "text/html"})

        def post(self, url, **kwargs):
            inner = json.loads(json.loads(kwargs["data"]["f.req"])[0][0][1])
            assert inner[0] == "garturlreq" and inner[2:] == ["CBMiabc", 123, "signature"]
            return types.SimpleNamespace(
                text=")]}'\n\n123\n"
                + json.dumps(
                    [["wrb.fr", "Fbv4je", json.dumps(["garturlres", publisher])], ["di", 1]]
                )
            )

    client = Client()
    item = _item(wrapper)
    assert fetch_item_content(client, item) == NEWS_BODY
    assert item.url == publisher
    assert item.dedupe_key == wrapper
    assert client.calls == [wrapper, publisher]


def test_google_news_missing_signature_does_not_extract_landing_page():
    client = FakeClient(text="<main>Google News consent boilerplate</main>")
    item = _item("https://news.google.com/rss/articles/CBMiabc")
    assert fetch_item_content(client, item) == ""
    assert len(client.calls) == 1


def test_google_news_rpc_failure_is_fail_soft():
    class Client(FakeClient):
        def post(self, *args, **kwargs):
            raise requests.exceptions.Timeout("timeout")

    client = Client(text='<div data-n-a-sg="sig" data-n-a-ts="123"></div>')
    assert fetch_item_content(client, _item("https://news.google.com/rss/articles/CBMiabc")) == ""


def test_google_news_invalid_rpc_results_are_not_articles():
    import json

    for payload in [
        None,
        json.dumps(["garturlres", "https://consent.google.com/"]),
        json.dumps(["garturlres", "javascript:alert(1)"]),
        "malformed",
    ]:

        class Client(FakeClient):
            def post(self, *args, **kwargs):
                return types.SimpleNamespace(
                    text=")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", payload]])
                )

        client = Client(text='<div data-n-a-sg="sig" data-n-a-ts="123"></div>')
        assert (
            fetch_item_content(client, _item("https://news.google.com/rss/articles/CBMiabc")) == ""
        )


def test_google_news_http_redirect_to_publisher():
    class Client(FakeClient):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            response.url = "https://publisher.example/article"
            return response

    item = _item("https://news.google.com/rss/articles/CBMiabc")
    assert fetch_item_content(Client(text=f"<article>{NEWS_BODY}</article>"), item) == NEWS_BODY
    assert item.url == "https://publisher.example/article"


@pytest.mark.parametrize(
    "html",
    [
        "<article><h1>Economic outlook</h1><p>A short teaser.</p></article>",
        "<main><h1>Subscribe to Brookings</h1><p>Our newsletters cover the economy. "
        + NEWS_BODY
        + "</p></main>",
        "<main><h1>Sign in to continue</h1><p>Subscribe to read this article.</p></main>",
        "<main><h1>Verify you are human</h1><p>Please enable JavaScript.</p></main>",
        "<article>"
        + "<p>Subscribe to read the full story and get unlimited access.</p>" * 20
        + "</article>",
        "<main><p>short teaser</p><div hidden>" + NEWS_BODY + "</div></main>",
        "<article><div class='paywall-message'><p>" + NEWS_BODY + "</p></div></article>",
    ],
)
def test_news_insufficient_or_access_only_pages_return_empty(html):
    assert extract_text(html, news=True) == ""


def test_news_removes_access_ui_but_keeps_available_reporting():
    html = (
        f"<article><p>{NEWS_BODY}</p><p>Subscribe to continue reading.</p>"
        f"<aside>newsletter sidebar {NEWS_BODY}</aside>"
        "<div role='dialog'><p>Sign in to read more.</p></div></article>"
    )
    assert extract_text(html, news=True) == NEWS_BODY


def test_news_about_subscriptions_is_not_rejected_by_keyword():
    story = (
        "Publishers are debating whether to subscribe to new distribution services. " + NEWS_BODY
    )
    assert extract_text(
        f"<article><h1>The economics of paywalls</h1><p>{story}</p></article>", news=True
    ).endswith(story)


def test_news_sufficiency_counts_unique_readable_text(monkeypatch):
    import services.scout.content as content

    monkeypatch.setattr(content, "SCOUT_NEWS_MIN_CONTENT_CHARS", 100)
    assert extract_text("<article>" + "<p>A tiny teaser.</p>" * 30 + "</article>", news=True) == ""
    assert extract_text("<article><p>" + "! " * 200 + "</p></article>", news=True) == ""


def test_news_threshold_is_configurable_and_applies_to_returned_budget(monkeypatch):
    import services.scout.content as content

    monkeypatch.setattr(content, "SCOUT_NEWS_MIN_CONTENT_CHARS", 10)
    assert extract_text("<article>abcdefghij</article>", news=True) == "abcdefghij"
    assert extract_text("<article>abcdefghij</article>", news=True, max_chars=9) == ""


def test_google_news_access_page_keeps_original_url_for_snippet_fallback(monkeypatch):
    import services.scout.content as content

    publisher = "https://publisher.example/economy"
    monkeypatch.setattr(content, "resolve_article_url", lambda *args: publisher)
    item = _item("https://news.google.com/rss/articles/test")
    assert (
        fetch_item_content(FakeClient(text="<main><h1>Subscribe to read</h1></main>"), item) == ""
    )
    assert item.url == publisher
    assert item.dedupe_key == "https://news.google.com/rss/articles/test"


def test_other_scout_sources_keep_short_content_contract():
    assert (
        fetch_item_content(FakeClient(text="<p>A small project README.</p>"), _item())
        == "A small project README."
    )
