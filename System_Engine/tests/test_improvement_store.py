"""Metacognition M3: improvement proposal store (queue + guarded approve)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.improvement_store import (
    make_proposal, save_proposal, list_proposals, get_proposal,
    approve_proposal, reject_proposal, unified_diff,
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
        axis="報告品質", target_path="Templates/Operations/synthesize.md",
        rationale="rc", addressed_fixes=["fix"],
        original_content=target.read_text(encoding="utf-8"),
        revised_content=revised, stamp="20260614120000",
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
    res = approve_proposal(p["id"], vault_dir=vault, pending_dir=d["pending_dir"],
                           applied_dir=d["applied_dir"], allowed_dirs=[vault / "Templates"])
    assert res["ok"]
    assert "ADDED" in target.read_text(encoding="utf-8")            # applied
    assert (d["applied_dir"] / f"{p['id']}.original.md").exists()   # backup
    assert get_proposal(p["id"], d["pending_dir"]) is None          # left the queue


def test_approve_refuses_if_target_changed(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target)
    save_proposal(p, d["pending_dir"])
    target.write_text("SOMEONE EDITED THIS\n", encoding="utf-8")     # concurrent edit
    res = approve_proposal(p["id"], vault_dir=vault, pending_dir=d["pending_dir"],
                           applied_dir=d["applied_dir"], allowed_dirs=[vault / "Templates"])
    assert not res["ok"] and "已被改動" in res["message"]
    assert target.read_text(encoding="utf-8") == "SOMEONE EDITED THIS\n"  # untouched


def test_approve_refuses_outside_allowlist(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    # Target a path escaping the allowlist.
    p = make_proposal(axis="x", target_path="../../etc/evil.md", rationale="r",
                      addressed_fixes=[], original_content="", revised_content="x",
                      stamp="20260614120000")
    save_proposal(p, d["pending_dir"])
    res = approve_proposal(p["id"], vault_dir=vault, pending_dir=d["pending_dir"],
                           applied_dir=d["applied_dir"], allowed_dirs=[vault / "Templates"])
    assert not res["ok"]


def test_approve_refuses_empty_revision(tmp_path):
    vault, target = _vault(tmp_path)
    d = _dirs(tmp_path)
    p = _prop(target, revised="   ")
    save_proposal(p, d["pending_dir"])
    res = approve_proposal(p["id"], vault_dir=vault, pending_dir=d["pending_dir"],
                           applied_dir=d["applied_dir"], allowed_dirs=[vault / "Templates"])
    assert not res["ok"]


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
