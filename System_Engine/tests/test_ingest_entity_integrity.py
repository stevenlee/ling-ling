from datetime import datetime, timezone

from services.ingest.entity_quality import assess_entity_body
from services.ingest.failure_ledger import IngestFailureLedger
from services.ingest.entity_audit import audit_part_tree


class TestEntityQualityGate:
    def test_rejects_reasoning_plan_leakage(self):
        result = assess_entity_body(
            "Source Material: harmonic numbers\n*   Goal: translate\n"
            "*   Constraints: preserve math\nDraft"
        )
        assert "reasoning_or_prompt_leakage" in result.hard_issues

    def test_rejects_yaml_contract_leaked_into_body(self):
        result = assess_entity_body("```yaml\ntitle: leaked\n```\nBody")
        assert "yaml_contract_leaked_into_body" in result.hard_issues

    def test_flags_part_137_style_corruption_as_suspect(self):
        result = assess_entity_body(
            'The value is 1/<h3>3 and then:\n```mermaid\ngraph TD\naltern變量["broken"]\n```'
        )
        assert "embedded_html_heading" in result.suspect_issues
        assert "corrupted_mermaid_identifier" in result.suspect_issues

    def test_allows_normal_markdown_and_mermaid(self):
        result = assess_entity_body(
            "## 定義\n調和數是部分和。\n```mermaid\ngraph TD\nA[收斂] --> B[界限]\n```"
        )
        assert result.clean


class TestIngestFailureLedger:
    def test_poison_content_is_quarantined_and_content_change_resets(self, tmp_path):
        ledger = IngestFailureLedger(
            tmp_path / "failures.json", max_attempts=2, quarantine_hours=24
        )
        source = tmp_path / "book.md"

        allowed, key, _ = ledger.begin(source, 3, "old chunk", "model-a")
        assert allowed
        ledger.fail(key, stage="entity_quality", detail="poison")
        allowed, key, _ = ledger.begin(source, 3, "old chunk", "model-a")
        assert allowed
        ledger.fail(key, stage="entity_quality", detail="poison")
        allowed, _, _ = ledger.begin(source, 3, "old chunk", "model-a")
        assert not allowed

        allowed, changed_key, _ = ledger.begin(source, 3, "new chunk", "model-a")
        assert allowed
        assert changed_key != key

    def test_outage_does_not_consume_attempt(self, tmp_path):
        path = tmp_path / "failures.json"
        ledger = IngestFailureLedger(path, max_attempts=1, quarantine_hours=24)
        source = tmp_path / "book.md"

        allowed, key, _ = ledger.begin(source, 1, "chunk", "model-a")
        assert allowed
        ledger.outage(key)

        reloaded = IngestFailureLedger(path, max_attempts=1, quarantine_hours=24)
        allowed, _, _ = reloaded.begin(source, 1, "chunk", "model-a")
        assert allowed

    def test_expired_quarantine_gets_only_one_half_open_probe(self, tmp_path):
        path = tmp_path / "failures.json"
        ledger = IngestFailureLedger(path, max_attempts=2, quarantine_hours=24)
        source = tmp_path / "book.md"
        _, key, _ = ledger.begin(source, 1, "chunk", "model-a")
        ledger.fail(key, stage="parse", detail="bad")
        _, key, _ = ledger.begin(source, 1, "chunk", "model-a")
        ledger.fail(key, stage="parse", detail="bad")
        ledger.state["failures"][key]["quarantined_until"] = datetime(
            2000, 1, 1, tzinfo=timezone.utc
        ).isoformat()
        ledger._save()

        allowed, key, _ = ledger.begin(source, 1, "chunk", "model-a")
        assert allowed
        ledger.fail(key, stage="parse", detail="still bad")
        allowed, _, _ = ledger.begin(source, 1, "chunk", "model-a")
        assert not allowed


def test_read_only_audit_finds_contract_and_semantic_poison(tmp_path):
    clean = tmp_path / "Book (Part 1).md"
    clean.write_text(
        "---\npart_digest: {thesis: ok}\ningest_status: complete\n---\n## 正常\n內容",
        encoding="utf-8",
    )
    yaml_leak = tmp_path / "Book (Part 2).md"
    yaml_leak.write_text(
        "---\npart_digest: {thesis: bad}\n---\n```yaml\ntitle: leaked\n```",
        encoding="utf-8",
    )
    semantic = tmp_path / "Book (Part 3).md"
    semantic.write_text(
        "---\npart_digest: {thesis: suspect}\n---\nValue 1/<h3>3",
        encoding="utf-8",
    )

    report = audit_part_tree(tmp_path)

    assert report["scanned"] == 3
    assert report["needs_reprocess"] == 2
    assert report["pages"][0]["status"] == "complete"
