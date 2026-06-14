"""Cross-lingual query expansion for retrieval.

The corpus is multilingual (Chinese / English / German). Vector + BM25
candidate generation is largely mono-lingual: a Chinese query rarely surfaces
the relevant English chunk into the pool, so the (multilingual) reranker never
gets to score it. We widen the net by translating the query into the other
corpus languages and retrieving for each variant; RRF fusion + rerank (against
the original query) then do precision. See `RAGManager.query_notes`.

This module is deliberately LLM-free: the translator is injected so the
retrieval layer stays free of provider coupling and the logic stays unit
testable with a stub.
"""
from __future__ import annotations

import logging
import re

# CJK unified ideographs (covers the Chinese we care about). Kana/Hangul are
# not in the corpus, so a CJK-ideograph ratio cleanly separates zh from en/de.
_CJK_RE = re.compile(r"[一-鿿]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def detect_lang(text: str) -> str:
    """Coarse language tag for routing: 'zh' if the query is meaningfully
    CJK, else 'en' (the latin-script bucket — en and de share it; German
    queries are rare and still benefit from an English/Chinese variant).

    Heuristic, not a classifier: we only need to pick which languages to
    translate *into*, and translation of an already-correct-language query is
    deduped away downstream, so a wrong call is cheap.
    """
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    if cjk == 0:
        return "en"
    letters = len(_LETTER_RE.findall(text))
    # Any non-trivial CJK presence routes as zh: a few Chinese chars in an
    # otherwise-English query still means the user is thinking cross-lingually.
    if cjk >= 2 or (letters and cjk / letters >= 0.2):
        return "zh"
    return "en"


def expand_queries(
    query: str,
    translator,
    target_langs: list[str],
    *,
    max_variants: int = 2,
) -> list[str]:
    """Return EXTRA query strings (not including the original) for cross-lingual
    retrieval — the original's translations into every target language other
    than its own.

    ``translator`` is a callable ``(text, langs: list[str]) -> dict[lang, str]``
    (e.g. ``LLMClient.translate_query``). Returns [] on any failure or when no
    target language differs from the query's — fail-open, never raises, so a
    translation outage degrades to plain mono-lingual retrieval.
    """
    if not query or not query.strip() or translator is None:
        return []
    src = detect_lang(query)
    wanted = [lang for lang in target_langs if lang != src][:max_variants]
    if not wanted:
        return []
    try:
        mapping = translator(query, wanted) or {}
    except Exception as e:  # noqa: BLE001 — degrade to mono-lingual retrieval
        logging.warning(f"Cross-lingual expansion failed, using original query only: {e}")
        return []

    out: list[str] = []
    seen = {query.strip()}
    for lang in wanted:
        variant = (mapping.get(lang) or "").strip()
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    return out
