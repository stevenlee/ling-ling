"""Part-digest markdown rendering (P2d).

Moved verbatim from IngestionPipeline.format_digest_appendix /
_format_one_digest. PART_DIGEST_HEADER is the shared section marker that the
resume/append/stitch logic keys on — one definition, imported everywhere.
"""

from __future__ import annotations

from core.utils import digest_value_to_text

PART_DIGEST_HEADER = "## 🧩 Part Digest Appendix"


def format_digest_appendix(part_digests: list) -> str:
    if not part_digests:
        return ""

    lines = [
        PART_DIGEST_HEADER,
        "",
        "> 每個 Part 的結構化摘要。這是 Ling Ling 進行總合成前的中間理解，可用來檢查 final synthesis 是否有根據。",
        "",
    ]

    for index, digest in enumerate(part_digests, 1):
        lines.extend(format_one_digest(index, digest))

    return "\n".join(lines).strip()


def format_one_digest(index: int, digest) -> list[str]:
    if isinstance(digest, str):
        return [f"### Part {index}", "", digest.strip(), ""]
    if not isinstance(digest, dict):
        return [f"### Part {index}", "", str(digest or "(empty digest)"), ""]

    def clean_list(values):
        if not values:
            return []
        if isinstance(values, str):
            values = [values]
        return [t for v in values if (t := digest_value_to_text(v))]

    def bullet_block(label: str, values) -> list[str]:
        items = clean_list(values)
        if not items:
            return []
        block = [f"- **{label}**:"]
        block.extend(f"  - {item}" for item in items)
        return block

    part_number = digest.get("part", index)
    title = digest.get("title") or f"Part {part_number}"
    out = [f"### Part {part_number}: {title}", ""]

    thesis = digest_value_to_text(digest.get("thesis", ""))
    if thesis:
        out.append(f"- **Thesis**: {thesis}")

    out.extend(bullet_block("Key Points", digest.get("key_points", [])))
    out.extend(bullet_block("Evidence", digest.get("evidence", [])))
    out.extend(bullet_block("Terms", digest.get("terms", [])))
    out.extend(bullet_block("Open Questions", digest.get("open_questions", [])))

    handoff = digest_value_to_text(digest.get("handoff", ""))
    if handoff:
        out.append(f"- **Handoff**: {handoff}")

    out.append("")
    return out
