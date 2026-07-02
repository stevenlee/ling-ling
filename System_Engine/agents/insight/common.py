"""Shared regexes/constants for the insight mixins (P2f, moved verbatim)."""

import re

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_HASHTAG_RE = re.compile(r"#([^\s#]+)")
_SKILL_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_BOOK_SUFFIX_RE = re.compile(r"\s*\((?:Part\s+\d+|Stitched|Synthesis)\)\s*$", re.IGNORECASE)
_STITCHED_SUFFIX_RE = re.compile(r"\(Stitched\)\s*$", re.IGNORECASE)
_SYNTHESIS_SUFFIX_RE = re.compile(r"\(Synthesis\)\s*$", re.IGNORECASE)

# Auto-attached by ingestion_pipeline.py — not content topics, so excluded
# from tag-cluster sampling (otherwise nearly every run picks one).
_SYSTEM_TAGS = frozenset({"synthesis", "completed", "stitched", "longform", "perfectpitch"})
_PLANNER_EXECUTE_MAX_STEPS = 4
