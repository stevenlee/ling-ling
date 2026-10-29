"""Resolve Google News article wrappers using the current signature/RPC contract."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from services.http_client import PoliteHttpClient

_RPC_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def is_google_news_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "news.google.com"


def _publisher_url(value: object) -> str:
    if isinstance(value, str):
        parts = urlparse(value)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme in ("http", "https")
            and host
            and not (host == "google.com" or host.endswith(".google.com"))
        ):
            return value
    raise ValueError("Google News 未提供有效的原文網址")


def resolve_article_url(client: PoliteHttpClient, url: str, headers: dict[str, str]) -> str:
    """Resolve one wrapper; never return Google consent/landing-page content."""
    path = urlparse(url).path
    match = re.fullmatch(r"/(?:rss/)?(?:articles|read)/([A-Za-z0-9_-]+)", path)
    if not match:
        raise ValueError("不支援的 Google News 文章網址")
    response = client.get(url, source="news.google.com", headers=headers, timeout=15, retries=1)
    final_url = getattr(response, "url", url)
    if not is_google_news_url(final_url):
        return _publisher_url(final_url)
    soup = BeautifulSoup(response.text, "lxml")
    node = soup.select_one("[data-n-a-sg][data-n-a-ts]")
    if node is None:
        raise ValueError("Google News 缺少原文解析參數")
    locale = [
        [
            "X",
            "X",
            ["X", "X"],
            None,
            None,
            1,
            1,
            "US:en",
            None,
            1,
            None,
            None,
            None,
            None,
            None,
            0,
            1,
        ],
        "X",
        "X",
        1,
        [1, 1, 1],
        1,
        1,
        None,
        0,
        0,
        None,
        0,
    ]
    inner = [
        "garturlreq",
        locale,
        match[1],
        int(str(node["data-n-a-ts"])),
        str(node["data-n-a-sg"]),
    ]
    rpc_headers = {
        **headers,
        "Referer": "https://news.google.com/",
        "Origin": "https://news.google.com",
        "X-Same-Domain": "1",
    }
    cookies = getattr(response, "cookies", None)
    if cookies:
        rpc_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    rpc = client.post(
        _RPC_URL,
        source="news.google.com",
        headers=rpc_headers,
        data={"f.req": json.dumps([[["Fbv4je", json.dumps(inner)]]])},
        timeout=15,
    )
    # Batchexecute may include byte-count lines and housekeeping frames.
    for line in rpc.text.splitlines():
        if not line.startswith("["):
            continue
        frames = json.loads(line)
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if (
                isinstance(frame, list)
                and len(frame) >= 3
                and frame[:2] == ["wrb.fr", "Fbv4je"]
                and isinstance(frame[2], str)
            ):
                result = json.loads(frame[2])
                if isinstance(result, list) and len(result) >= 2 and result[0] == "garturlres":
                    return _publisher_url(result[1])
    raise ValueError("Google News 原文解析未回傳網址")
