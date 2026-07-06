"""C2: correct_code_identifiers — safe snap-back for code identifiers."""

from services.code_identifier_guard import correct_code_identifiers

CANON = ["RecallAgent", "_load_prompt", "correct_identifiers"]


def test_snaps_casing_and_separator_inside_backticks():
    text = "The `recallagent` calls `recall_agent` and `Load_Prompt`."
    out, fixes = correct_code_identifiers(text, CANON)
    assert "`RecallAgent`" in out
    assert "`_load_prompt`" in out
    assert "recallagent" not in out and "recall_agent" not in out
    assert len(fixes) == 3


def test_prose_outside_backticks_untouched():
    # Same normalized form, but not in backticks → must NOT be rewritten.
    text = "The recall agent handles memory."
    out, fixes = correct_code_identifiers(text, CANON)
    assert out == text
    assert fixes == []


def test_already_correct_is_no_op():
    text = "`RecallAgent` is fine and `_load_prompt` too."
    out, fixes = correct_code_identifiers(text, CANON)
    assert out == text
    assert fixes == []


def test_non_distinctive_canonical_is_not_protected():
    # A plain lowercase name collides with ordinary words → skip it.
    out, fixes = correct_code_identifiers("run `main` now", ["main"])
    assert out == "run `main` now"
    assert fixes == []


def test_ambiguous_normalized_collision_is_dropped():
    # Two canonicals normalizing to the same form → unsafe to pick → skip both.
    out, fixes = correct_code_identifiers("see `foobar`", ["fooBar", "foo_bar"])
    assert out == "see `foobar`"
    assert fixes == []


def test_multi_token_backtick_span_untouched():
    # Not a single identifier token → leave it alone.
    out, fixes = correct_code_identifiers("call `recall_agent.execute()`", CANON)
    assert out == "call `recall_agent.execute()`"
    assert fixes == []
