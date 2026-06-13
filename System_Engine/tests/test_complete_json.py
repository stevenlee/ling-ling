"""R4: _complete_json — unified reasoning-channel re-roll for JSON calls."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import json
import pytest

from services.llm_client import LLMClient


def _client(replies):
    """LLMClient whose _complete_text yields `replies` in order (str or Exception)."""
    c = LLMClient.__new__(LLMClient)
    calls = {"n": 0, "kwargs": []}

    def fake(**kw):
        calls["kwargs"].append(kw)
        i = calls["n"]
        calls["n"] += 1
        val = replies[i] if i < len(replies) else replies[-1]
        if isinstance(val, Exception):
            raise val
        return val

    c._complete_text = fake
    c._calls = calls
    return c


# ── object kind ──────────────────────────────────────────────────────

def test_object_valid_first_try():
    c = _client([json.dumps({"verdict": "keep"})])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {"verdict": "keep"}
    assert c._calls["n"] == 1


def test_object_parse_miss_then_recovers():
    c = _client(["thinking out loud, no json", json.dumps({"ok": 1})])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {"ok": 1}
    assert c._calls["n"] == 2


def test_object_literal_empty_no_reroll():
    # A literal {} is a genuine empty answer — must not burn a second call.
    c = _client(["{}"])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {}
    assert c._calls["n"] == 1


def test_array_substring_bracket_is_not_genuine_empty_rerolls():
    # Audit B1: a parse-miss reply that merely *contains* "[]" as a substring
    # (wrong shape) must re-roll, not be accepted as a genuine empty array.
    c = _client(['{"items": []}', json.dumps([{"a": 1}])])
    out = c._complete_json(kind="array", system_prompt="s", user_msg="u")
    assert out == [{"a": 1}]
    assert c._calls["n"] == 2


def test_array_fenced_empty_is_genuine_no_reroll():
    # A fenced/padded literal [] is still a genuine empty — no re-roll.
    c = _client(["```json\n[]\n```"])
    out = c._complete_json(kind="array", system_prompt="s", user_msg="u")
    assert out == []
    assert c._calls["n"] == 1


def test_object_two_misses_returns_empty():
    c = _client(["nope", "still nope"])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {}
    assert c._calls["n"] == 2


# ── array kind ───────────────────────────────────────────────────────

def test_array_valid_first_try():
    c = _client([json.dumps([{"claim": "x"}])])
    out = c._complete_json(kind="array", system_prompt="s", user_msg="u")
    assert out == [{"claim": "x"}]
    assert c._calls["n"] == 1


def test_array_literal_empty_no_reroll():
    c = _client(["[]"])
    out = c._complete_json(kind="array", system_prompt="s", user_msg="u")
    assert out == []
    assert c._calls["n"] == 1


def test_array_miss_then_recovers():
    c = _client(["garbage", json.dumps([{"a": 1}])])
    out = c._complete_json(kind="array", system_prompt="s", user_msg="u")
    assert out == [{"a": 1}]
    assert c._calls["n"] == 2


# ── exception handling ───────────────────────────────────────────────

def test_exception_then_recovers():
    c = _client([RuntimeError("transient"), json.dumps({"ok": 1})])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {"ok": 1}
    assert c._calls["n"] == 2


def test_exception_both_attempts_returns_empty():
    c = _client([RuntimeError("down"), RuntimeError("still down")])
    out = c._complete_json(kind="object", system_prompt="s", user_msg="u")
    assert out == {}
    assert c._calls["n"] == 2


# ── trace metadata passthrough ───────────────────────────────────────

def test_stamps_json_attempt_in_trace_metadata():
    c = _client(["miss", json.dumps({"ok": 1})])
    c._complete_json(
        kind="object", system_prompt="s", user_msg="u",
        trace_context={"stage": "demo", "metadata": {"k": "v"}},
    )
    # Two attempts → two stamps, preserving the caller's own metadata.
    m0 = c._calls["kwargs"][0]["trace_context"]["metadata"]
    m1 = c._calls["kwargs"][1]["trace_context"]["metadata"]
    assert m0 == {"k": "v", "json_attempt": 1}
    assert m1 == {"k": "v", "json_attempt": 2}
