"""Version-locked task prompts (P2b).

Moved verbatim from services/llm_client.py; names de-underscored. The
versioning contract is unchanged: NEVER edit an existing version key —
regression baselines depend on byte-identical prompt text.
"""

# ─── Quality scorer prompts (version-locked) ──────────────────────────
#
# Prompt text is locked in code so regression runs across days/weeks see the
# same prompt. To change a prompt, add a new version key — never edit an
# existing version after it's been used to produce baseline scores.

CHUNK_COHERENCE_PROMPTS: dict[str, str] = {
    "v1": (
        "You are evaluating how self-contained a text chunk is for use as a "
        "retrieval unit in a knowledge base. Score 1-10:\n"
        "- 10: Reads as a complete, standalone thought. No dangling references.\n"
        "- 7-9: Mostly self-contained, minor context-dependence.\n"
        "- 4-6: Somewhat broken at the start or end mid-thought.\n"
        "- 1-3: Severely fragmented; cannot stand alone.\n\n"
        "Return ONLY a JSON object with this exact schema:\n"
        '{"score": <integer 1-10>, "reason": "<one short sentence>"}\n\n'
        "Do not include any text outside the JSON object."
    ),
}

# Topic-shift detection (Phase 4 of the Thoughtful Splitter).
#
# LLMs hallucinate character offsets but reliably handle PARAGRAPH INDICES
# — so the contract is "split_after paragraph N", not "split at offset N".
# The splitter converts indices back to source offsets deterministically.
#
# IMPORTANT: never edit an existing version after baseline outputs exist.
# To change a prompt, add a new version key.

# Context summary (Phase 5 of the Thoughtful Splitter).
#
# A 1-2 sentence factual summary of the previous chunk, used as a context
# preamble for the next chunk. Replaces structural overlap when enabled.
# Must match the source language (Chinese in, Chinese out).
#
# Same versioning rules: never edit an existing version.

SUMMARY_PROMPTS: dict[str, str] = {
    "v1": (
        "You are generating a brief context preamble. A reader is about to read a section\n"
        "of text and you must hand them the gist of the section that came IMMEDIATELY before,\n"
        "so they can pick up the thread.\n\n"
        "Rules:\n"
        "- 1 to 2 sentences, total length ≤ 200 characters.\n"
        "- Match the INPUT LANGUAGE exactly (Chinese in → Chinese out; English in → English out).\n"
        '- Write declarative facts. No "As we saw...", "This passage discusses...", or other meta framing.\n'
        "- Preserve key proper nouns, names, terms, and the conclusion.\n"
        "- Do not invent facts that the input doesn't support.\n\n"
        "Return ONLY a JSON object with this exact schema:\n"
        '  {"summary": "<1-2 sentences, ≤ 200 chars>"}\n\n'
        "No prose, no markdown, no commentary outside the JSON."
    ),
}


TOPIC_SHIFT_PROMPTS: dict[str, str] = {
    "v1": (
        "You are an editor segmenting a long passage of prose into self-contained sections.\n"
        "The passage has no chapter headings — it flows through one or more topics paragraph by paragraph.\n\n"
        "You will receive numbered paragraphs. Identify 0, 1, or 2 paragraph boundaries where the topic\n"
        "**clearly shifts** to a substantially new idea (not just a sub-point, restatement, or example).\n\n"
        "Return ONLY a single JSON object with this exact schema:\n"
        '  {"split_after": [<paragraph_number>, ...]}\n\n'
        "Examples:\n"
        '  - All paragraphs continue one topic → {"split_after": []}\n'
        '  - Paragraphs 1-3 about topic A, 4-N about topic B → {"split_after": [3]}\n'
        '  - Three distinct topics with shifts after P3 and P6 → {"split_after": [3, 6]}\n\n'
        "Rules:\n"
        "- Each value must be between 1 and (N-1) inclusive. You cannot split before the first\n"
        "  paragraph or after the last.\n"
        "- Maximum 2 entries. If you detect more than 2 shifts, return only the 2 strongest.\n"
        "- Only a **clear topic shift** counts — a paragraph that opens a substantially new line\n"
        "  of thought, not one that elaborates or rephrases the previous one.\n"
        "- Output ONLY the JSON object. No prose, no markdown, no commentary."
    ),
}
