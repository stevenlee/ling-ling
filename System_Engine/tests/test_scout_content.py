"""services/scout/content.py — best-effort item-content extraction."""

import types

import requests

from services.scout.content import MAX_CONTENT_CHARS, extract_text, fetch_item_content
from services.scout.models import CrawledItem


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


def test_prefilled_content_skips_fetch():
    client = FakeClient()
    assert fetch_item_content(client, _item(content="already here")) == "already here"
    assert client.calls == []


def test_network_error_and_non_html_return_empty():
    boom = FakeClient(error=requests.exceptions.ConnectionError("boom"))
    assert fetch_item_content(boom, _item()) == ""
    pdf = FakeClient(text="%PDF-1.7", content_type="application/pdf")
    assert fetch_item_content(pdf, _item()) == ""
