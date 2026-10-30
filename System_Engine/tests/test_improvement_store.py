"""Metacognition M3: improvement proposal store (queue + guarded approve)."""

import os
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.improvement_store import (
    make_proposal,
    save_proposal,
    list_proposals,
    get_proposal,
    approve_proposal,
    reject_proposal,
    unified_diff,
)


def _vault(tmp_path):
    """A tiny vault with one tunable asset and the queue dirs."""
    vault = tmp_path / "vault"
    assets = vault / "Templates" / "Operations"
    assets.mkdir(parents=True)
    target = assets / "synthesize.md"
    target.write_text("original prompt\nline two\n", encoding="utf-8")
    return vault, target


def _dirs(tmp_path):
    return dict(
        pending_dir=tmp_path / "q" / "_pending",
        applied_dir=tmp_path / "q" / "_applied",
        rejected_dir=tmp_path / "q" / "_rejected",
    )


def _prop(target, revised="original prompt\nline two\nADDED\n"):
    return make_proposal(
        axis="報告品質",
        target_path="Templates/Operations/synthesize.md",
        rationale="rc",
        addressed_fixes=["fix"],
        original_content=target.read_text(encoding="utf-8"),
        revised_content=revised,
        stamp="20260614120000",
    )


def test_save_list_get_roundtrip(tmp_path):
    _, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    assert [x["id"] for x in list_proposals(d["pending_dir"])] == [p["id"]]
    assert get_proposal(p["id"], d["pending_dir"])["target_path"].endswith("synthesize.md")


def test_approve_applies_and_backs_up(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )
    assert res["ok"]
    assert "ADDED" in target.read_text(encoding="utf-8")  # applied
    assert (d["applied_dir"] / f"{p['id']}.original.md").exists()  # backup
    assert get_proposal(p["id"], d["pending_dir"]) is None  # left the queue


def test_approve_refuses_if_target_changed(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    target.write_text("SOMEONE EDITED THIS\n", encoding="utf-8")  # concurrent edit
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )
    assert not res["ok"] and "已被改動" in res["message"]
    assert target.read_text(encoding="utf-8") == "SOMEONE EDITED THIS\n"  # untouched


def test_approve_refuses_outside_allowlist(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    # Target a path escaping the allowlist.
    p = make_proposal(
        axis="x",
        target_path="../../etc/evil.md",
        rationale="r",
        addressed_fixes=[],
        original_content="",
        revised_content="x",
        stamp="20260614120000",
    )
    save_proposal(p, d["pending_dir"])
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )
    assert not res["ok"]


def test_approve_refuses_empty_revision(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target, revised="   ")
    save_proposal(p, d["pending_dir"])
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )
    assert not res["ok"]


def test_approve_target_replace_failure_leaves_live_prompt_unchanged(tmp_path, monkeypatch):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    original = target.read_text(encoding="utf-8")

    real_replace = Path.replace

    def fail_target_replace(self, destination):
        if Path(destination) == target:
            raise OSError("simulated target replace failure")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_target_replace)
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )

    assert not res["ok"]
    assert target.read_text(encoding="utf-8") == original
    assert get_proposal(p["id"], d["pending_dir"]) is not None


def test_approve_cleanup_failure_reports_applied_state(tmp_path, monkeypatch):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    pending_path = save_proposal(p, d["pending_dir"])

    real_unlink = Path.unlink

    def fail_pending_unlink(self, *args, **kwargs):
        if self == pending_path:
            raise OSError("simulated queue cleanup failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_unlink)
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )

    assert res["ok"] and res["cleanup_required"]
    assert "ADDED" in target.read_text(encoding="utf-8")
    assert "已生效" in res["message"]


def test_reject_moves_out_of_queue(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    res = reject_proposal(p["id"], pending_dir=d["pending_dir"], rejected_dir=d["rejected_dir"])
    assert res["ok"]
    assert get_proposal(p["id"], d["pending_dir"]) is None
    assert (d["rejected_dir"] / f"{p['id']}.json").exists()


def test_diff_shows_added_line(tmp_path):
    _, target = _vault(tmp_path)
    assert "ADDED" in unified_diff(_prop(target))


def test_reject_persists_reason_and_legacy_record_still_loads(tmp_path):
    _, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    res = reject_proposal(
        p["id"],
        pending_dir=d["pending_dir"],
        rejected_dir=d["rejected_dir"],
        reason="  新增欄位並沒有 parser 支援。  ",
    )
    assert res["ok"]
    rejected = get_proposal(p["id"], d["rejected_dir"])
    assert rejected["rejected_reason"] == "新增欄位並沒有 parser 支援。"
    assert "rejected_at" in rejected


def test_reject_record_failure_preserves_pending(tmp_path, monkeypatch):
    _, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    pending = save_proposal(p, d["pending_dir"])
    original = pending.read_bytes()
    real_replace = Path.replace

    def fail_rejection_replace(self, destination):
        if Path(destination).parent == d["rejected_dir"]:
            raise OSError("rejected disk full")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_rejection_replace)
    res = reject_proposal(
        p["id"], pending_dir=d["pending_dir"], rejected_dir=d["rejected_dir"], reason="why"
    )
    assert not res["ok"] and "保留待審" in res["message"]
    assert pending.read_bytes() == original
    assert not list(d["rejected_dir"].iterdir())


def test_reject_cleanup_failure_is_committed_and_retry_preserves_reason(tmp_path, monkeypatch):
    _, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    pending = save_proposal(p, d["pending_dir"])
    real_unlink = Path.unlink

    def fail_pending_unlink(self, *args, **kwargs):
        if self == pending:
            raise OSError("queue busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending_unlink)
    res = reject_proposal(
        p["id"], pending_dir=d["pending_dir"], rejected_dir=d["rejected_dir"], reason="keep reason"
    )
    assert res["ok"] and res["cleanup_required"]
    assert "已退回" in res["message"] and pending.exists()
    assert get_proposal(p["id"], d["rejected_dir"])["rejected_reason"] == "keep reason"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = reject_proposal(p["id"], pending_dir=d["pending_dir"], rejected_dir=d["rejected_dir"])
    assert retry["ok"] and not pending.exists()
    assert get_proposal(p["id"], d["rejected_dir"])["rejected_reason"] == "keep reason"


def test_save_id_collision_does_not_overwrite_existing_proposal(tmp_path):
    import pytest

    _, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    path = save_proposal(p, d["pending_dir"])
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_proposal({**p, "revised_content": "other edit"}, d["pending_dir"])
    assert path.read_bytes() == original


def test_approval_checks_json_contract_for_legacy_pending_proposal(tmp_path):
    vault, _ = _vault(tmp_path)
    target = vault / "Templates/Prompts/cortex_falsifiability.md"
    target.parent.mkdir(parents=True)
    original = '{"score": "float", "falsifier": "text", "falsifier_zh": "text"}'
    target.write_text(original, encoding="utf-8")
    p = make_proposal(
        axis="Cortex 信念",
        target_path="Templates/Prompts/cortex_falsifiability.md",
        rationale="legacy",
        addressed_fixes=[],
        original_content=original,
        revised_content=original[:-1] + ', "needs_enrichment": "boolean"}',
    )
    d = _dirs(tmp_path)
    pending = save_proposal(p, d["pending_dir"])
    res = approve_proposal(
        p["id"],
        vault_dir=vault,
        pending_dir=d["pending_dir"],
        applied_dir=d["applied_dir"],
        allowed_dirs=[vault / "Templates"],
    )
    assert not res["ok"] and "JSON 契約" in res["message"]
    assert target.read_text(encoding="utf-8") == original
    assert pending.exists() and not d["applied_dir"].exists()


def test_review_memory_preserves_edit_order_and_meaningful_whitespace():
    from services.improvement_review import edit_key, diagnosis_key

    one = [{"find": "a\r\nb", "replace": "c", "why": "old"}]
    assert edit_key(one) == edit_key([{"find": "a\nb", "replace": "c", "why": "new"}])
    assert edit_key(one) != edit_key([{"find": "a b", "replace": "c"}])
    two = [{"find": "a", "replace": "b"}, {"find": "b", "replace": "c"}]
    assert edit_key(two) != edit_key(list(reversed(two)))
    assert edit_key([]) is None
    assert diagnosis_key("a  b", ["x", "y"]) == diagnosis_key("a b", ["y", "x"])


def test_contract_preserves_required_keys_and_allows_description_edits():
    from services.improvement_review import prompt_contract_error

    target = "Templates/Prompts/cortex_falsifiability.md"
    original = '{"score": "float", "falsifier": "text", "falsifier_zh": "text"}'
    assert not prompt_contract_error(
        target, original, original.replace('"text"', '"concrete observation"')
    )
    assert prompt_contract_error(target, original, '{"score": "float"}')
    assert prompt_contract_error(target, original, original[:-1] + ', "新欄位": "ignored"}')
