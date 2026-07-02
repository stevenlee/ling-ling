"""PoliteHttpClient — throttled, retrying HTTP GET for external sources (P1).

Consolidates what research_pipeline hand-rolled: per-source politeness
intervals (arXiv asks for ~3s between API hits; Wikipedia 429s bursts; FPO is
scraped so it gets a human-ish cadence), a descriptive User-Agent, and the
429/transient retry schedule. One instance per pipeline — the throttle state
(last-request-at per source) lives on the instance.
"""

from __future__ import annotations

import time

import requests

from core.retrying import retry_call

# Politeness: minimum seconds between consecutive requests to the SAME external
# source. The research pipeline hits each source once (or N times) per keyword
# in a tight 5-keyword loop; without spacing that burst looks like a scraper
# and earns a 429 (FPO rate-limited the patent burst; Wikipedia's API 429'd
# the search+extract burst). Values reflect each service's tolerance.
DEFAULT_MIN_INTERVALS = {
    "fpo": 1.33,
    "wikipedia": 1.0,
    "arxiv": 3.0,
}

# Wikipedia's User-Agent policy wants a descriptive UA with a contact. Use the
# public repo URL as contact rather than a personal email (this string ships in
# a public repo). arXiv/FPO are happy with it too, so all sources share it.
RESEARCH_USER_AGENT = "LingLingResearchBot/1.0 (+https://github.com/stevenlee/ling-ling)"


class PoliteHttpClient:
    def __init__(
        self,
        min_intervals: dict[str, float] | None = None,
        *,
        default_interval: float = 1.0,
        user_agent: str = RESEARCH_USER_AGENT,
    ):
        self.min_intervals = dict(DEFAULT_MIN_INTERVALS if min_intervals is None else min_intervals)
        self.default_interval = default_interval
        self.user_agent = user_agent
        self._last_req_at: dict[str, float] = {}  # source -> monotonic time of last request

    def throttle(self, source: str) -> None:
        """Self-rate-limit requests to ``source`` to its politeness interval so
        a tight per-keyword loop doesn't look like a scraper."""
        interval = self.min_intervals.get(source, self.default_interval)
        wait = self._last_req_at.get(source, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_req_at[source] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        source: str,
        headers: dict | None = None,
        timeout: int = 20,
        retries: int = 3,
    ) -> requests.Response:
        """Throttled GET with retry: exponential backoff on HTTP 429, fixed
        backoff on other transient network errors; other HTTP errors raise
        immediately. Caller headers win over the default User-Agent (FPO is
        fetched with a browser UA, for example)."""
        merged = {"User-Agent": self.user_agent, **(headers or {})}

        def _is_retryable(e: Exception) -> bool:
            if isinstance(e, requests.exceptions.HTTPError):
                status = e.response.status_code if e.response is not None else None
                return status == 429
            return isinstance(e, requests.exceptions.RequestException)

        def _delay(attempt: int, e: Exception) -> float:
            if isinstance(e, requests.exceptions.HTTPError):
                return 2 ** (attempt - 1) + 2  # 429: exponential
            return 2.0  # other transient network errors: fixed

        def _get():
            resp = requests.get(url, headers=merged, timeout=timeout)
            resp.raise_for_status()
            return resp

        self.throttle(source)
        return retry_call(
            _get, retries=retries, is_retryable=_is_retryable, delay_fn=_delay, jitter=0
        )
