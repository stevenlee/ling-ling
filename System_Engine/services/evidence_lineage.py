"""Read-only document provenance closure for A2; no model or index access.

Disjointness means disjoint *recorded original documents*, not independent
experiments/authors. Missing, ambiguous or cyclic ancestry never proves it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.config import EVIDENCE_LINEAGE_MAX_DEPTH, EVIDENCE_LINEAGE_MAX_NODES
from services.evidence_contract import PASSAGE_LIMIT
from core.parser import parse_markdown_metadata, strip_body_frontmatter
from core.vault_utils import sanitize_filename
from services.cortex_store import active_evidence, parse_cortex_page

_SOURCE_KEYS = ("related_docs", "related_titles", "target_titles", "sources")
_VARIANT = re.compile(r"\s+\((?:Part\s+\d+|Stitched|Synthesis)\)$", re.IGNORECASE)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc or parts.username:
            return ""
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
        )
    except ValueError:
        return ""


def _references(meta: dict) -> tuple[list[str], bool]:
    refs: list[str] = []
    valid = True
    for key in _SOURCE_KEYS:
        if key not in meta:
            continue
        items = meta[key]
        if not isinstance(items, list) or not all(isinstance(v, str) and v.strip() for v in items):
            valid = False
        else:
            refs.extend(items)
    unique = sorted(set(refs))
    return unique[:EVIDENCE_LINEAGE_MAX_NODES], valid and len(unique) <= EVIDENCE_LINEAGE_MAX_NODES


@dataclass
class _Closure:
    roots: set[str] = field(default_factory=set)
    families: set[str] = field(default_factory=set)
    claims: set[str] = field(default_factory=set)
    nodes: dict[str, dict] = field(default_factory=dict)
    reasons: set[str] = field(default_factory=set)

    def merge(self, other: _Closure) -> None:
        self.roots.update(other.roots)
        self.families.update(other.families)
        self.claims.update(other.claims)
        self.nodes.update(other.nodes)
        self.reasons.update(other.reasons)

    def record(self) -> dict:
        return {
            "roots": sorted(self.roots),
            "families": sorted(self.families),
            "claims": sorted(self.claims),
            "nodes": [self.nodes[k] for k in sorted(self.nodes)],
            "reasons": sorted(self.reasons),
            "complete": bool(self.roots) and not self.reasons,
        }


def _status(candidate: dict, origin: dict) -> str:
    if any(set(candidate[key]) & set(origin[key]) for key in ("roots", "families", "claims")):
        return "shared"
    if candidate["complete"] and origin["complete"]:
        return "independent"
    return "unknown"


def validate_lineage(value: object) -> dict:
    """Validate persisted closure shape; never upgrade a legacy missing record."""
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("來源 lineage 版本無效")
    for side in ("candidate", "origin"):
        manifest = value.get(side)
        if not isinstance(manifest, dict) or type(manifest.get("complete")) is not bool:
            raise ValueError("來源 lineage 缺少 closure")
        for key in ("roots", "families", "claims", "reasons"):
            if not isinstance(manifest.get(key), list) or not all(
                isinstance(v, str) and v for v in manifest[key]
            ):
                raise ValueError(f"來源 lineage {key} 無效")
        nodes = manifest.get("nodes")
        if not isinstance(nodes, list) or not all(
            isinstance(node, dict)
            and isinstance(node.get("path"), str)
            and isinstance(node.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", node["sha256"])
            for node in nodes
        ):
            raise ValueError("來源 lineage nodes 無效")
        if manifest["complete"] != (bool(manifest["roots"]) and not manifest["reasons"]):
            raise ValueError("來源完整性與 root／缺口不符")
    if value.get("status") != _status(value["candidate"], value["origin"]):
        raise ValueError("來源 lineage 判定與 closure 不符")
    return value


class SourceLineageResolver:
    """Bounded per-batch filesystem snapshot. Apply creates a fresh resolver."""

    def __init__(self, vault_dir: Path):
        self.vault = vault_dir.resolve()
        self._paths: list[Path] | None = None
        self._reads: dict[Path, str] = {}
        self._visits = 0
        self._claim_paths: dict[str, list[Path]] | None = None

    def _inventory(self) -> list[Path]:
        if self._paths is None:
            paths = []
            for name in ("pages", "Notes", "Insights", "Cortex", "raw"):
                for path in (self.vault / name).rglob("*.md"):
                    try:
                        resolved = path.resolve()
                        resolved.relative_to(self.vault / name)
                        paths.append(resolved)
                    except (ValueError, OSError, RuntimeError):
                        continue  # symlinks outside the allowed root are not ancestry
            self._paths = paths
        return self._paths

    def _resolve(self, reference: str) -> Path | None:
        ref = reference.strip()
        if ref.startswith("[[") and ref.endswith("]]"):
            ref = ref[2:-2].split("|")[0].split("#")[0]
        path = Path(ref.replace("\\", "/"))
        if path.suffix != ".md":
            path = path.with_name(path.name + ".md")
        # Explicit paths never fall back to a same-named unrelated note.
        if path.is_absolute() or len(path.parts) > 1:
            resolved = (path if path.is_absolute() else self.vault / path).resolve()
            if resolved in self._inventory():
                return resolved
            return None
        if ref.startswith("cortex-"):
            if self._claim_paths is None:
                claims: dict[str, list[Path]] = {}
                for item in self._inventory():
                    if item.relative_to(self.vault).parts[0] != "Cortex":
                        continue
                    # claim_id is independent of the human-readable filename.
                    meta = parse_markdown_metadata(item.read_text(encoding="utf-8"))
                    identifier = meta.get("claim_id")
                    if isinstance(identifier, str) and identifier:
                        claims.setdefault(identifier, []).append(item)
                self._claim_paths = claims
            matches = self._claim_paths.get(ref, [])
            return matches[0] if len(matches) == 1 else None
        matches = [p for p in self._inventory() if p.name == path.name]
        return matches[0] if len(matches) == 1 else None

    def _visit(self, reference: str, trail: tuple[Path, ...] = ()) -> _Closure:
        result = _Closure()
        self._visits += 1
        if self._visits > EVIDENCE_LINEAGE_MAX_NODES:
            result.reasons.add("來源遍歷次數超過上限")
            return result
        try:
            external = _url(reference)
            if external:
                result.roots.add("url:" + external)
                result.reasons.add(f"URL 僅能提示同源，缺少原始文件快照：{external}")
                return result
            path = self._resolve(reference)
            if path is None:
                result.reasons.add(f"來源不存在或不唯一：{reference}")
                return result
            relative = path.relative_to(self.vault).as_posix()
            result.families.add("file:" + relative)
            if path in trail or len(trail) >= EVIDENCE_LINEAGE_MAX_DEPTH:
                result.reasons.add(f"來源循環或深度上限：{relative}")
                return result
            if path not in self._reads:
                self._reads[path] = path.read_text(encoding="utf-8")
            text = self._reads[path]
            meta = parse_markdown_metadata(text)
            refs, valid = _references(meta)
            grounded = meta.get("grounded_on", [])
            if not isinstance(grounded, list) or not all(
                isinstance(v, str) and v.strip() for v in grounded
            ):
                valid, grounded = False, []
            if len(grounded) > EVIDENCE_LINEAGE_MAX_NODES:
                valid, grounded = False, grounded[:EVIDENCE_LINEAGE_MAX_NODES]
            result.nodes[relative] = {
                "path": relative,
                "sha256": _hash(text),
                "references": refs,
                "grounded_on": sorted(set(grounded)),
            }
            if not valid:
                result.reasons.add(f"來源 metadata schema 無效：{relative}")
            following = (*trail, path)
            if relative.startswith("Cortex/"):
                page = parse_cortex_page(path, text=text)
                if page is None:
                    result.reasons.add(f"Cortex 無法解析：{relative}")
                else:
                    result.merge(self._origin(page, following))
                return result
            if relative.startswith("raw/"):
                body, _ = strip_body_frontmatter(text)
                if not body.strip():
                    result.reasons.add(f"原始文件為空：{relative}")
                else:
                    result.roots.update(("raw:" + relative, "body-sha256:" + _hash(body)))
                    for key in ("url", "source_url", "source"):
                        url = _url(meta.get(key))
                        if url:
                            result.roots.add("url:" + url)
                return result
            # Known ingestion layout gives a shared family hint, but no positive
            # independence without a unique archived original or explicit refs.
            base = _VARIANT.sub("", path.stem)
            if relative.startswith("pages/") and path.parent.name == base and base != path.stem:
                result.families.add("document:" + path.parent.relative_to(self.vault).as_posix())
                if not refs:
                    originals = [
                        p
                        for p in self._inventory()
                        if p.relative_to(self.vault).parts[0] == "raw"
                        and sanitize_filename(re.sub(r"_\d{8}-\d{6}$", "", p.stem)) == base
                    ]
                    if len(originals) == 1:
                        refs.append(originals[0].relative_to(self.vault).as_posix())
                    result.reasons.add(f"文件命名僅是同源提示，缺少明確來源引用：{relative}")
            for key in ("source_url", "url"):
                url = _url(meta.get(key))
                if url:
                    refs.append(url)
            result.nodes[relative]["references"] = sorted(set(refs))
            if not refs and not grounded:
                result.reasons.add(f"缺少原始來源：{relative}")
            for ref in sorted(set(refs)):
                result.merge(self._visit(ref, following))
            for claim_id in grounded:
                result.claims.add(claim_id)
                result.merge(self._visit(claim_id, following))
        except (OSError, ValueError, RuntimeError) as error:
            result.reasons.add(f"來源讀取失敗：{reference}（{type(error).__name__}）")
        return result

    def _origin(self, page, trail: tuple[Path, ...] = ()) -> _Closure:
        result = _Closure(claims={page.claim_id})
        entries = active_evidence(page)
        if not entries:
            result.reasons.add("主張沒有可追溯的 active evidence")
        if len(entries) > EVIDENCE_LINEAGE_MAX_NODES:
            result.reasons.add("主張 evidence 數量超過來源遍歷上限")
        for entry in entries[:EVIDENCE_LINEAGE_MAX_NODES]:
            # Retained A04 provenance survives source deletion. Its complete
            # candidate closure is an audit snapshot, not a title assertion.
            stored = entry.get("source_lineage")
            if stored is not None:
                try:
                    validated = validate_lineage(stored)
                    if validated["status"] != "independent":
                        raise ValueError("舊佐證 lineage 不具獨立來源資格")
                    manifest = validated["candidate"]
                    for key in ("roots", "families", "claims", "reasons"):
                        getattr(result, key).update(manifest[key])
                    result.nodes.update({node["path"]: node for node in manifest["nodes"]})
                except (ValueError, TypeError):
                    result.reasons.add("既有 evidence lineage 損壞")
            else:
                insight = entry.get("insight")
                if (
                    isinstance(insight, str)
                    and insight
                    and not insight.startswith("[EvidenceTrace]")
                ):
                    # evidence.insight is in the Insights namespace; a missing
                    # insight must never resolve to a raw file with that name.
                    ref = insight if "/" in insight or "\\" in insight else "Insights/" + insight
                    result.merge(self._visit(ref, trail))
                else:
                    result.reasons.add("既有 evidence 缺少可驗證的 insight／lineage")
            for key in ("sources", "grounded_on"):
                if key == "sources" and stored is not None:
                    continue  # display titles are not ancestry when an audited closure exists
                refs = entry.get(key, [])
                if not isinstance(refs, list) or not all(
                    isinstance(ref, str) and ref.strip() for ref in refs
                ):
                    result.reasons.add(f"既有 evidence {key} schema 無效")
                    continue
                if len(refs) > EVIDENCE_LINEAGE_MAX_NODES:
                    result.reasons.add(f"既有 evidence {key} 數量超過上限")
                for ref in refs[:EVIDENCE_LINEAGE_MAX_NODES]:
                    if key == "grounded_on":
                        result.claims.add(ref)
                    result.merge(self._visit(ref, trail))
        return result

    def compare(self, page, hit: dict) -> dict:
        # Budget is per comparison; cached bytes still belong to this batch.
        self._visits = 0
        meta = hit.get("metadata") or {}
        path = meta.get("source_path") or meta.get("source")
        candidate = (
            self._visit(path)
            if isinstance(path, str) and path
            else _Closure(reasons={"候選缺少來源路徑"})
        )
        try:
            local = self._resolve(path) if isinstance(path, str) and path else None
            text = hit.get("text")
            if (
                local is None
                or not isinstance(text, str)
                or not text.strip()
                or text[:PASSAGE_LIMIT] not in self._reads.get(local, "")
            ):
                candidate.reasons.add("檢索片段不在可驗證的目前來源檔案中")
        except (ValueError, OSError, RuntimeError):
            candidate.reasons.add("無法驗證檢索片段的來源版本")
        origin = self._origin(page)
        left, right = candidate.record(), origin.record()
        return {
            "schema_version": 1,
            "status": _status(left, right),
            "candidate": left,
            "origin": right,
        }
