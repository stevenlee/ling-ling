"""Scout deep-dig — follow a single URL one level deeper, on demand.

The daily digest reads each item's own page and stops. `@ling-dig <url>`
goes further for the ONE thing the user cares about: fetch the page, let the
LLM pick the few outbound links actually worth following (docs, the paper,
the discussion — not nav chrome), fetch those too, and synthesize everything
into one deep-dive note. Human picks the target; no crawl budget is wasted
on items nobody asked about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests

from services.http_client import PoliteHttpClient
from services.scout.content import BROWSER_HEADERS, extract_text

MAIN_MAX_CHARS = 16000
LINKED_MAX_CHARS = 5000
MAX_FOLLOW_LINKS = 4
MAX_CANDIDATE_LINKS = 25

_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`]+")
_NUMBER_RE = re.compile(r"\d+")
# Obvious chrome URLs never reach the LLM candidate list — live smoke showed
# gemma picking github's login?return_to=… despite the prompt saying not to.
_CHROME_URL_RE = re.compile(
    r"/(login|signin|sign[-_]?in|signup|sign[-_]?up|register|logout|join|subscribe|"
    r"cart|checkout|privacy|terms|cookie)s?\b|[?&](return_to|redirect|ref_?src)=",
    re.IGNORECASE,
)

_SELECT_SYSTEM = (
    "You are Scout's deep-dig operator. The user gives you the text of a web "
    "page plus a numbered list of links found on it. Pick AT MOST "
    f"{MAX_FOLLOW_LINKS} links that would genuinely deepen understanding of "
    "the page's core subject — documentation, the underlying paper, source "
    "code, the discussion thread. Skip navigation, ads, login, social, and "
    "'related articles' chrome. Reply with ONLY the chosen numbers separated "
    "by commas (e.g. `2, 5, 9`), or `NONE` if nothing is worth following."
)

_SYNTHESIZE_SYSTEM = (
    "You are Scout, writing a deep-dive intelligence note in {language}. The "
    "user gives you a main page plus the content of a few followed links. "
    "Structure the note as `##` sections (localized headings): (1) what this "
    "is, in two sentences; (2) the key substance — concrete points grounded "
    "in the main page; (3) what the followed links add — attribute each point "
    "to its link by name; (4) open questions worth pursuing. Ground every "
    "claim in the provided text; say so when the material is thin. No preamble."
)


@dataclass
class DigSource:
    label: str
    url: str
    content: str = ""
    error: str | None = None


@dataclass
class DigResult:
    status: str
    summary: str
    title: str = ""
    body: str = ""
    followed: list[DigSource] = field(default_factory=list)


def first_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(".,;:!?）)】>」") if match else None


def run_dig(
    llm,
    url: str,
    *,
    language: str,
    client: PoliteHttpClient | None = None,
) -> DigResult:
    client = client or PoliteHttpClient({}, default_interval=1.0)

    try:
        response = client.get(url, source=_host(url), headers=dict(BROWSER_HEADERS), timeout=20)
    except requests.exceptions.RequestException as e:
        return DigResult("failed", f"主頁抓取失敗：{e}")
    html = response.text
    main_text = extract_text(html, max_chars=MAIN_MAX_CHARS)
    if not main_text:
        return DigResult("failed", "主頁抓到了但抽不出可讀內文（可能是 JS 渲染頁）。")

    title = _page_title(html) or url
    candidates = extract_links(html, base_url=url)
    followed = [_fetch_linked(client, link) for link in _select_links(llm, main_text, candidates)]

    body = _synthesize(llm, title, url, main_text, followed, language)
    fetched = sum(1 for s in followed if s.content)
    return DigResult(
        "succeeded",
        f"深掘完成：主頁 + 跟進 {fetched}/{len(followed)} 條連結。",
        title=title,
        body=body,
        followed=followed,
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "dig").lower()


def _page_title(html: str) -> str:
    from bs4 import BeautifulSoup

    tag = BeautifulSoup(html, "lxml").find("title")
    return re.sub(r"\s+", " ", tag.get_text()).strip() if tag else ""


def extract_links(html: str, *, base_url: str) -> list[DigSource]:
    """Outbound candidate links: absolute http(s), meaningful anchor text,
    deduped, capped. Same-page fragments and the page itself are dropped."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[DigSource] = []
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text()).strip()
        href = urljoin(base_url, str(a["href"]).strip())
        if not href.startswith(("http://", "https://")):
            continue
        href = href.split("#", 1)[0]
        if not href or href == base_url or href in seen or len(text) < 4:
            continue
        if _CHROME_URL_RE.search(href):
            continue
        seen.add(href)
        links.append(DigSource(label=text[:120], url=href))
        if len(links) >= MAX_CANDIDATE_LINKS:
            break
    return links


def _select_links(llm, main_text: str, candidates: list[DigSource]) -> list[DigSource]:
    if not candidates:
        return []
    numbered = "\n".join(f"{i}. {c.label} — {c.url}" for i, c in enumerate(candidates, start=1))
    user_msg = f"## Page text (truncated)\n{main_text[:4000]}\n\n## Links\n{numbered}"
    raw = llm.complete(_SELECT_SYSTEM, user_msg, stage="dig_select") or ""
    if "NONE" in raw.upper():
        return []
    picked = []
    for number in _NUMBER_RE.findall(raw):
        index = int(number)
        if 1 <= index <= len(candidates) and candidates[index - 1] not in picked:
            picked.append(candidates[index - 1])
        if len(picked) >= MAX_FOLLOW_LINKS:
            break
    return picked


def _fetch_linked(client: PoliteHttpClient, link: DigSource) -> DigSource:
    try:
        response = client.get(
            link.url, source=_host(link.url), headers=dict(BROWSER_HEADERS), timeout=15, retries=1
        )
        link.content = extract_text(response.text, max_chars=LINKED_MAX_CHARS)
        if not link.content:
            link.error = "抽不出可讀內文"
    except requests.exceptions.RequestException as e:
        link.error = str(e)
        logging.info(f"Scout dig: linked fetch failed for {link.url}: {e}")
    return link


def _synthesize(
    llm, title: str, url: str, main_text: str, followed: list[DigSource], language: str
) -> str:
    parts = [f"# Main page: {title}\nURL: {url}\n\n{main_text}"]
    for source in followed:
        if source.content:
            parts.append(f"# Followed link: {source.label}\nURL: {source.url}\n\n{source.content}")
    raw = llm.complete(
        _SYNTHESIZE_SYSTEM.format(language=language),
        "\n\n---\n\n".join(parts),
        stage="dig_synthesize",
    )
    return (raw or "").strip() or "（深掘分析產生失敗——LLM 未回覆。）"
