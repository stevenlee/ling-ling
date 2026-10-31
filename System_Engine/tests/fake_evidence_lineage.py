"""Known, distinct provenance for transport/schema/index transaction unit tests.

Real filesystem closure and unknown/shared gates are exercised separately in
 test_evidence_lineage.py; this double does not replace those integration tests.
"""


class KnownLineageResolver:
    def __init__(self, vault_dir):
        self.vault_dir = vault_dir

    def compare(self, page, hit):
        meta = hit.get("metadata") or {}
        key = meta.get("source_path") or meta.get("title") or "candidate"
        if meta.get("title") == "OriginDoc (Synthesis)":
            key = "origin"
        candidate = {
            "roots": ["fixture:" + key],
            "families": [],
            "claims": [],
            "nodes": [],
            "reasons": [],
            "complete": True,
        }
        origin = {
            "roots": ["fixture:origin"],
            "families": [],
            "claims": [page.claim_id],
            "nodes": [],
            "reasons": [],
            "complete": True,
        }
        for entry in page.evidence:
            if isinstance(entry, dict) and isinstance(entry.get("source_lineage"), dict):
                origin["roots"] = sorted(
                    set(origin["roots"]) | set(entry["source_lineage"]["candidate"]["roots"])
                )
        return {
            "schema_version": 1,
            "status": "shared" if set(candidate["roots"]) & set(origin["roots"]) else "independent",
            "candidate": candidate,
            "origin": origin,
        }
