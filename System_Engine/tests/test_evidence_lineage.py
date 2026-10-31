"""Real filesystem lineage closures and end-to-end A2 apply gates, all isolated."""

import copy
from datetime import datetime
from pathlib import Path

import pytest

from core.parsing.markdown_metadata import dump_markdown_with_metadata
from services.cortex_store import CortexPage, parse_cortex_page, save_cortex_page
from services.evidence_audit import load_audit, text_hash
from services.evidence_lineage import SourceLineageResolver, validate_lineage
from maintenance import evidence_traceback as traceback

_NOW = datetime(2026, 10, 31, 10)
_PASSAGE = "指定條件下的原文證據。"


def _write(root, name, meta=None, body=_PASSAGE):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_markdown_with_metadata(meta or {}, body), encoding="utf-8")
    return path


def _candidate(root, name="candidate", source="raw/other.md", **meta):
    _write(root, f"Notes/{name}.md", {"sources": [source], **meta})
    return {
        "metadata": {"title": name, "source_path": f"Notes/{name}.md"},
        "text": _PASSAGE,
        "distance": 0.2,
    }


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    _write(root, "raw/origin.md", body="原始觀察 A")
    _write(root, "raw/other.md", body="原始觀察 B")
    _write(root, "Insights/origin.md", {"related_docs": ["raw/origin.md"]})
    page = CortexPage(
        "cortex-one",
        root / "Cortex/human-readable-name.md",
        "測試主張",
        falsifier="測試反例",
        confidence=0.5,
        S=1,
        last_reinforced_at="2026-01-01T00:00:00",
        evidence=[
            {
                "insight": "origin",
                "sources": ["raw/origin.md"],
                "date": "2026-01-01",
                "summary": "原始佐證",
            }
        ],
    )
    save_cortex_page(page)
    monkeypatch.setattr(traceback.settings, "EVIDENCE_TRACEBACK_APPLY", True)
    monkeypatch.setattr(traceback.settings, "EVIDENCE_TRACEBACK_BATCH", 5)
    monkeypatch.setattr(traceback.settings, "EVIDENCE_TRACEBACK_MAX_DISTANCE", 0.45)
    return root, page


def _compare(vault, hit):
    root, page = vault
    return SourceLineageResolver(root).compare(page, hit)


class FakeRAG:
    def __init__(self, hits):
        self.hits, self.indexed = hits, []

    def query_notes(self, query, **kwargs):
        return copy.deepcopy(self.hits)

    def add_document(self, path, title, text, **kwargs):
        self.indexed.append(title)

    def add_facets(self, *args, **kwargs):
        return True


class FakeLLM:
    def __init__(self, callback=None):
        self.calls, self.callback = 0, callback

    def judge_evidence_scope(self, *, claim, falsifier, title, passage, claim_id):
        self.calls += 1
        if self.callback:
            self.callback()
        return {
            "contract_version": 2,
            "relation": "full_support",
            "reason": "指定情境的直接支持",
            "quotes": [passage],
            "unsupported": [],
            "applicability": "指定條件",
            "audit_context": {
                "provider": "fake",
                "model": "fake-model",
                "prompt_asset": "fake",
                "prompt_body": "fake prompt",
                "prompt_sha256": text_hash("fake prompt"),
                "contract_version": 2,
                "scope_attempt": 1,
                "trace_run_id": None,
            },
        }


def _run(vault, hits, llm=None):
    root, _ = vault
    rag = FakeRAG(hits)
    result = traceback.run_evidence_traceback(
        llm or FakeLLM(),
        rag,
        cortex_dir=root / "Cortex",
        out_dir=root / "fromLingLing",
        state_file=root / "Database/cursor.json",
        repair_dir=root / "Database/repairs",
        audit_dir=root / "Database/audits",
        vault_dir=root,
        now=_NOW,
    )
    return result, rag


def test_distinct_recorded_originals_are_independent(vault):
    root, _ = vault
    lineage = _compare(vault, _candidate(root))
    assert lineage["status"] == "independent"
    assert lineage["candidate"]["complete"] and lineage["origin"]["complete"]
    assert "raw:raw/other.md" in lineage["candidate"]["roots"]
    assert validate_lineage(lineage) == lineage


@pytest.mark.parametrize("view", ["Part 1", "Part 9", "Stitched", "Synthesis"])
def test_views_with_different_titles_share_original(vault, view):
    root, page = vault
    _write(root, "pages/Book/Book (Synthesis).md", {"sources": ["raw/origin.md"]})
    _write(root, f"pages/Book/Book ({view}).md", {"sources": ["raw/origin.md"]})
    _write(root, "Insights/origin.md", {"related_docs": ["pages/Book/Book (Synthesis).md"]})
    hit = {
        "metadata": {
            "title": "completely different display title",
            "source_path": f"pages/Book/Book ({view}).md",
        },
        "text": _PASSAGE,
    }
    assert _compare(vault, hit)["status"] == "shared"
    result, rag = _run(vault, [{**hit, "distance": 0.2}])
    assert result.scans[0].excluded_same_origin == 1
    assert result.scans[0].judgments == [] and rag.indexed == []
    assert len(parse_cortex_page(page.path).evidence) == 1


def test_filename_convention_is_only_a_negative_hint(vault):
    root, _ = vault
    _write(root, "raw/Book.md", body="原文")
    _write(root, "pages/Book/Book (Part 1).md", {"part": 1})
    hit = {"metadata": {"source_path": "pages/Book/Book (Part 1).md"}, "text": _PASSAGE}
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert "raw:raw/Book.md" in record["candidate"]["roots"]
    assert any("命名僅是同源提示" in reason for reason in record["candidate"]["reasons"])


def test_identical_original_body_under_different_filenames_is_shared(vault):
    root, _ = vault
    _write(root, "raw/copy.md", {"title": "別名"}, body="原始觀察 A")
    assert _compare(vault, _candidate(root, source="raw/copy.md"))["status"] == "shared"


def test_same_original_url_is_shared_across_revisions_and_fragments(vault):
    root, _ = vault
    _write(root, "raw/origin.md", {"url": "https://EXAMPLE.com/paper#first"}, body="版本 A")
    _write(root, "raw/other.md", {"source_url": "https://example.com/paper#second"}, body="版本 B")
    assert _compare(vault, _candidate(root))["status"] == "shared"


def test_different_url_queries_are_not_silently_collapsed(vault):
    root, _ = vault
    _write(root, "raw/origin.md", {"url": "https://example.com/paper?id=1"}, body="版本 A")
    _write(root, "raw/other.md", {"url": "https://example.com/paper?id=2"}, body="版本 B")
    assert _compare(vault, _candidate(root))["status"] == "independent"


def test_all_insight_source_fields_are_followed_without_first_field_or_five_item_cap(vault):
    root, _ = vault
    refs = []
    for i in range(6):
        ref = f"raw/extra-{i}.md"
        _write(root, ref, body=f"觀察 {i}")
        refs.append(ref)
    _write(root, "Insights/origin.md", {"related_docs": refs[:3], "sources": refs[3:]})
    assert _compare(vault, _candidate(root, source=refs[-1]))["status"] == "shared"


def test_grounded_on_claim_id_resolves_human_readable_cortex_filename(vault):
    root, page = vault
    other = copy.deepcopy(page)
    other.claim_id, other.path = "cortex-other", root / "Cortex/另一個人工檔名.md"
    save_cortex_page(other)
    hit = _candidate(root, grounded_on=[other.claim_id])
    record = _compare(vault, hit)
    assert record["status"] == "shared"
    assert other.claim_id in record["candidate"]["claims"]
    assert "raw:raw/origin.md" in record["candidate"]["roots"]


def test_direct_self_grounding_is_rejected(vault):
    root, page = vault
    assert _compare(vault, _candidate(root, grounded_on=[page.claim_id]))["status"] == "shared"


def test_duplicate_claim_id_makes_grounding_unknown(vault):
    root, page = vault
    other = copy.deepcopy(page)
    other.claim_id, other.path = "cortex-duplicate", root / "Cortex/a.md"
    save_cortex_page(other)
    other.path = root / "Cortex/b.md"
    save_cortex_page(other)
    hit = _candidate(root, grounded_on=[other.claim_id])
    assert _compare(vault, hit)["status"] == "unknown"


@pytest.mark.parametrize(
    "defect", ["missing-insight", "missing-original", "ambiguous", "invalid-schema", "cycle"]
)
def test_incomplete_ancestry_is_unknown_and_cannot_strengthen(vault, defect):
    root, page = vault
    hit = _candidate(root)
    if defect == "missing-insight":
        (root / "Insights/origin.md").unlink()
    elif defect == "missing-original":
        (root / "raw/other.md").unlink()
    elif defect == "ambiguous":
        _write(root, "Notes/duplicate.md", {"sources": ["raw/other.md"]})
        _write(root, "pages/duplicate.md", {"sources": ["raw/other.md"]})
        _write(root, "Notes/candidate.md", {"sources": ["duplicate"]})
    elif defect == "invalid-schema":
        _write(root, "Notes/candidate.md", {"sources": "raw/other.md"})
    else:
        _write(root, "Notes/a.md", {"sources": ["Notes/candidate.md"]})
        _write(root, "Notes/candidate.md", {"sources": ["Notes/a.md"]})
    before = page.path.read_bytes()
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    result, rag = _run(vault, [hit])
    assert result.status == "ok"  # unknown ancestry is a reportable outcome, not transport failure
    assert page.path.read_bytes() == before and rag.indexed == []
    assert "原始文件來源關係：unknown" in result.report_path.read_text(encoding="utf-8")


def test_explicit_missing_path_never_falls_back_to_same_basename(vault):
    root, _ = vault
    _write(root, "Notes/unique.md", {"sources": ["raw/other.md"]})
    hit = _candidate(root, source="pages/missing/unique.md")
    assert _compare(vault, hit)["status"] == "unknown"


def test_stale_retrieved_passage_cannot_be_paired_with_current_lineage(vault):
    root, _ = vault
    hit = _candidate(root)
    hit["text"] = "已被修改且不在目前來源中的舊片段"
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert any("檢索片段" in reason for reason in record["candidate"]["reasons"])


def test_symlink_outside_vault_is_not_read(vault, tmp_path):
    root, _ = vault
    outside = root.parent / f"{root.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "Notes").mkdir(exist_ok=True)
        (root / "Notes/escape.md").symlink_to(outside)
        record = _compare(vault, _candidate(root, source="Notes/escape.md"))
        assert record["status"] == "unknown"
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.parametrize("limit", ["EVIDENCE_LINEAGE_MAX_DEPTH", "EVIDENCE_LINEAGE_MAX_NODES"])
def test_traversal_limits_fail_to_unknown_not_independent(vault, monkeypatch, limit):
    root, _ = vault
    monkeypatch.setattr(f"services.evidence_lineage.{limit}", 1)
    assert _compare(vault, _candidate(root))["status"] == "unknown"


def test_source_io_failure_is_unknown_and_preserves_other_closure_information(vault, monkeypatch):
    root, _ = vault
    hit = _candidate(root)
    read = Path.read_text

    def fail_original(path, *args, **kwargs):
        if path == root / "raw/other.md":
            raise OSError("unreadable raw")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_original)
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert "raw:raw/origin.md" in record["origin"]["roots"]


def test_real_lineage_is_audited_preserved_and_only_strengthens_once(vault):
    root, page = vault
    result, rag = _run(vault, [_candidate(root)])
    assert result.status == "ok" and rag.indexed == [page.claim_id]
    reloaded = parse_cortex_page(page.path)
    assert len(reloaded.evidence) == 2 and reloaded.confidence == 0.55
    entry = reloaded.evidence[-1]
    assert entry["source_lineage"]["status"] == "independent"
    saved = load_audit(root / "Database/audits", entry["audit_id"])
    assert saved["payload"]["source"]["lineage"] == entry["source_lineage"]
    save_cortex_page(reloaded)
    assert parse_cortex_page(page.path).evidence[-1]["source_lineage"] == entry["source_lineage"]
    second, _ = _run(vault, [_candidate(root)])
    assert second.scans == []
    assert parse_cortex_page(page.path).confidence == 0.55


def test_two_views_of_one_original_are_one_candidate(vault):
    root, page = vault
    hits = [_candidate(root, name="alias-a"), _candidate(root, name="alias-b")]
    llm = FakeLLM()
    result, _ = _run(vault, hits, llm)
    assert llm.calls == 1
    assert result.scans[0].excluded_duplicate_origin == 1
    assert result.scans[0].applied["added_evidence"] == 1
    assert len(parse_cortex_page(page.path).evidence) == 2


@pytest.mark.parametrize("moment", ["during-judge", "after-intent"])
def test_changed_source_lineage_before_commit_is_rejected(vault, monkeypatch, moment):
    root, page = vault
    hit = _candidate(root)
    before = page.path.read_bytes()

    def change():
        _write(root, "Notes/candidate.md", {"sources": ["raw/origin.md"]})

    if moment == "during-judge":
        llm = FakeLLM(callback=change)
    else:
        llm = FakeLLM()
        enqueue = traceback.enqueue_index_repair

        def change_after_intent(*args):
            slot = enqueue(*args)
            change()
            return slot

        monkeypatch.setattr(traceback, "enqueue_index_repair", change_after_intent)
    result, rag = _run(vault, [hit], llm)
    assert result.status == "failed"
    assert "lineage" in result.scans[0].applied["error"]
    assert page.path.read_bytes() == before and rag.indexed == []


def test_audited_original_closure_survives_display_title_or_source_deletion(vault):
    root, page = vault
    result, _ = _run(vault, [_candidate(root)])
    assert result.status == "ok"
    reloaded = parse_cortex_page(page.path)
    reloaded.evidence = [reloaded.evidence[-1]]
    reloaded.evidence[0]["sources"] = ["不再存在的顯示名稱"]
    (root / "Notes/candidate.md").unlink()
    (root / "raw/other.md").unlink()
    _write(root, "raw/new.md", body="原始觀察 C")
    hit = _candidate(root, "next", "raw/new.md")
    record = SourceLineageResolver(root).compare(reloaded, hit)
    assert record["status"] == "independent"
    assert "raw:raw/other.md" in record["origin"]["roots"]


def test_invalid_stored_lineage_is_not_trusted(vault):
    root, page = vault
    hit = _candidate(root)
    record = _compare(vault, hit)
    record["candidate"]["roots"] = []
    page.evidence[0]["source_lineage"] = record
    assert _compare(vault, hit)["status"] == "unknown"


def test_superseded_evidence_does_not_poison_current_origin(vault):
    root, page = vault
    page.evidence.append(
        {"insight": "missing", "sources": ["raw/other.md"], "superseded_by": "revision"}
    )
    assert _compare(vault, _candidate(root))["status"] == "independent"


def test_two_distinct_urls_alone_do_not_prove_independent_originals(vault):
    root, _ = vault
    _write(root, "Notes/candidate.md", {"sources": ["https://example.com/other"]})
    hit = {
        "metadata": {"title": "candidate", "source_path": "Notes/candidate.md"},
        "text": _PASSAGE,
    }
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert "url:https://example.com/other" in record["candidate"]["roots"]


def test_archive_collision_suffix_prevents_unique_filename_hint(vault):
    root, _ = vault
    _write(root, "raw/Book.md", body="舊原文")
    _write(root, "raw/Book_20261031-100000.md", body="新原文")
    _write(root, "pages/Book/Book (Synthesis).md")
    hit = {"metadata": {"source_path": "pages/Book/Book (Synthesis).md"}, "text": _PASSAGE}
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert not record["candidate"]["roots"]


def test_apply_rejects_a_forged_independent_status_over_unknown_ancestry(vault):
    root, page = vault
    hit = _candidate(root)
    (root / "raw/other.md").unlink()
    recorded = _compare(vault, hit)
    assert recorded["status"] == "unknown"
    forged = copy.deepcopy(recorded)
    forged["status"] = "independent"
    verdict = FakeLLM().judge_evidence_scope(
        claim=page.claim,
        falsifier=page.falsifier,
        title="candidate",
        passage=_PASSAGE,
        claim_id=page.claim_id,
    )
    context = verdict.pop("audit_context")
    judgment = traceback.PassageJudgment(
        "candidate",
        passage=_PASSAGE,
        source_path="Notes/candidate.md",
        source_family="candidate",
        source_lineage=forged,
        audit_context=context,
        **verdict,
    )
    scan = traceback.ClaimScan(
        page.claim_id,
        page.claim,
        page.falsifier,
        judgments=[judgment],
        basis_snapshot=page.path.read_text(encoding="utf-8"),
        basis_path=str(page.path),
    )
    before = page.path.read_bytes()
    result = traceback._apply_scan(
        page,
        scan,
        FakeRAG([]),
        _NOW,
        audit_dir=root / "Database/audits",
        repair_dir=root / "Database/repairs",
        vault_dir=root,
    )
    assert "error" in result
    assert page.path.read_bytes() == before


def test_two_different_originals_thicken_twice_but_reinforce_once(vault):
    root, page = vault
    _write(root, "raw/third.md", body="原始觀察 C")
    result, _ = _run(
        vault, [_candidate(root, name="one"), _candidate(root, name="two", source="raw/third.md")]
    )
    assert result.status == "ok"
    assert result.scans[0].applied["added_evidence"] == 2
    reloaded = parse_cortex_page(page.path)
    assert len(reloaded.evidence) == 3
    assert reloaded.confidence == 0.55


def test_same_display_title_for_distinct_originals_does_not_merge_evidence_identity(vault):
    root, page = vault
    _write(root, "raw/third.md", body="原始觀察 C")
    hits = [_candidate(root, name="one"), _candidate(root, name="two", source="raw/third.md")]
    for hit in hits:
        hit["metadata"]["title"] = "相同顯示標題"
    result, _ = _run(vault, hits)
    assert result.scans[0].applied["added_evidence"] == 2
    markers = [e["insight"] for e in parse_cortex_page(page.path).evidence[1:]]
    assert len(set(markers)) == 2
    assert all(marker.startswith("[EvidenceTrace] 相同顯示標題 [origin:") for marker in markers)


def test_shared_original_is_rejected_even_if_audited_claim_stays_thin(vault):
    root, page = vault
    _run(vault, [_candidate(root)])
    reloaded = parse_cortex_page(page.path)
    reloaded.evidence = [reloaded.evidence[-1]]
    save_cortex_page(reloaded)
    before = page.path.read_bytes()
    hit = _candidate(root, name="different-title")
    result, _ = _run(vault, [hit])
    assert result.scans[0].excluded_same_origin == 1
    assert result.scans[0].judgments == []
    assert page.path.read_bytes() == before


def test_reference_fanout_is_bounded_and_does_not_hide_missing_sources(vault, monkeypatch):
    root, _ = vault
    monkeypatch.setattr("services.evidence_lineage.EVIDENCE_LINEAGE_MAX_NODES", 3)
    hit = _candidate(root)
    _write(root, "Notes/candidate.md", {"sources": [f"raw/missing-{i}.md" for i in range(100)]})
    record = _compare(vault, hit)
    assert record["status"] == "unknown"
    assert len(record["candidate"]["reasons"]) <= 5


@pytest.mark.parametrize("known", [False, True])
def test_contradiction_keeps_lineage_gate_and_never_reinforces(vault, known):
    root, page = vault
    hit = _candidate(root)
    if not known:
        (root / "raw/other.md").unlink()

    class ContradictionLLM(FakeLLM):
        def judge_evidence_scope(self, **kwargs):
            verdict = super().judge_evidence_scope(**kwargs)
            verdict["relation"] = "contradicts"
            return verdict

    result, _ = _run(vault, [hit], ContradictionLLM())
    reloaded = parse_cortex_page(page.path)
    assert result.scans[0].applied["tensions"] == int(known)
    assert reloaded.S == page.S and reloaded.confidence == page.confidence
    assert len(reloaded.evidence) == 1
    if known:
        assert "[origin:" in reloaded.counterpoints[0]


def test_distinct_original_counterpoints_with_same_title_are_both_preserved(vault):
    root, page = vault
    _write(root, "raw/third.md", body="反例觀察 C")
    hits = [_candidate(root, name="one"), _candidate(root, name="two", source="raw/third.md")]
    for hit in hits:
        hit["metadata"]["title"] = "相同標題"

    class ContradictionLLM(FakeLLM):
        def judge_evidence_scope(self, **kwargs):
            verdict = super().judge_evidence_scope(**kwargs)
            verdict["relation"] = "contradicts"
            return verdict

    result, _ = _run(vault, hits, ContradictionLLM())
    assert result.scans[0].applied["tensions"] == 2
    assert len(parse_cortex_page(page.path).counterpoints) == 2
