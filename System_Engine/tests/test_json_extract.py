"""core/json_extract.py — lenient JSON extraction (P1).

The pre-move behavior stays pinned by test_parser.py (facade re-exports) and
test_llm_client.py (_parse_json_array); this file covers the new home + the
seams that matter for callers.
"""

from core.json_extract import (
    extract_json_array,
    extract_json_object,
    is_empty_json_literal,
    salvage_json_array,
)


def test_array_fenced_and_raw():
    assert extract_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert extract_json_array('noise before [{"a": 1}] noise after') == [{"a": 1}]


def test_array_filters_non_dict_items():
    assert extract_json_array('["s", {"a": 1}, 3]') == [{"a": 1}]


def test_array_repairs_illegal_latex_escapes():
    assert extract_json_array(r'[{"claim": "$\Delta \chi^2$ shrinks"}]') == [
        {"claim": "$\\Delta \\chi^2$ shrinks"}
    ]


def test_repairs_latex_shaped_valid_escapes():
    # `\forall` starts with the VALID escape `\f` — json.loads used to decode
    # it into `\x0c orall` silently (NaN-poisoned embeddings, line-split
    # truncation in facet backfill). Same family: \neq, \tan, \binom, \rho.
    raw = r'{"kp": "$a^n \equiv a \pmod n, \forall a \neq 0$: \tan, \binom{n}{k}, \rho"}'
    parsed = extract_json_object(raw)
    assert parsed == {
        "kp": "$a^n \\equiv a \\pmod n, \\forall a \\neq 0$: \\tan, \\binom{n}{k}, \\rho"
    }


def test_repair_keeps_non_latex_shaped_control_escapes():
    # Repair path (the \pmod makes strict parse fail): a control escape NOT
    # followed by a lowercase letter is a genuine newline/tab and survives.
    raw = r'{"a": "one\nTwo\n2nd", "b": "$x \pmod y$"}'
    assert extract_json_object(raw) == {"a": "one\nTwo\n2nd", "b": "$x \\pmod y$"}


def test_strict_valid_json_is_never_altered():
    # No illegal escape anywhere → strict parse wins; `\n` before a lowercase
    # letter stays a newline (the LaTeX-shape heuristic only runs on repair).
    raw = r'{"a": "one\ntwo", "tex": "$\\forall x$"}'
    assert extract_json_object(raw) == {"a": "one\ntwo", "tex": "$\\forall x$"}


def test_repair_keeps_already_escaped_backslashes():
    # `\\forall` is correct JSON already — the repair must not double the
    # second backslash of an escaped pair (`\\f…` is not a `\f` collision).
    raw = r'{"tex": "$\\forall x$", "bad": "\pmod"}'
    assert extract_json_object(raw) == {"tex": "$\\forall x$", "bad": "\\pmod"}


def test_object_embedded_in_prose():
    assert extract_json_object('The answer is {"score": 0.5} as shown.') == {"score": 0.5}


def test_object_empty_on_garbage():
    assert extract_json_object("no json here") == {}
    assert extract_json_array("") == []


def test_salvage_survives_truncated_tail():
    truncated = '[{"idx": 1, "relevance": "高"}, {"idx": 2, "relev'
    assert salvage_json_array(truncated) == [{"idx": 1, "relevance": "高"}]


def test_salvage_skips_single_bad_entry():
    bad_middle = '[{"idx": 1}, {idx: broken}, {"idx": 3}]'
    assert salvage_json_array(bad_middle) == [{"idx": 1}, {"idx": 3}]


def test_salvage_fast_path_keeps_non_dict_items():
    # Unlike extract_json_array, the fast path is unfiltered — callers that
    # expect string arrays rely on this.
    assert salvage_json_array('["a", "b"]') == ["a", "b"]


def test_empty_literal_detection():
    assert is_empty_json_literal("[]")
    assert is_empty_json_literal("```json\n[]\n```")
    assert is_empty_json_literal("{}", kind="object")
    assert not is_empty_json_literal('{"items": []}')
    assert not is_empty_json_literal("", kind="array")
