"""
Unit tests for core.utils — shared utility functions.
"""

import pytest
from core.utils import digest_value_to_text


class TestDigestValueToText:
    def test_none(self):
        assert digest_value_to_text(None) == ""

    def test_string(self):
        assert digest_value_to_text("  hello  ") == "hello"

    def test_integer(self):
        assert digest_value_to_text(42) == "42"

    def test_float(self):
        assert digest_value_to_text(3.14) == "3.14"

    def test_list(self):
        assert digest_value_to_text(["a", "b", "c"]) == "a; b; c"

    def test_tuple(self):
        assert digest_value_to_text(("x", "y")) == "x; y"

    def test_set(self):
        # Sets are unordered, so just check it returns something non-empty
        result = digest_value_to_text({"a"})
        assert result == "a"

    def test_dict(self):
        result = digest_value_to_text({"key": "val"})
        assert '"key"' in result and '"val"' in result

    def test_nested_list(self):
        result = digest_value_to_text(["hello", ["inner", "list"]])
        assert "hello" in result
        assert "inner" in result

    def test_filters_empty_strings(self):
        result = digest_value_to_text(["", "  ", "valid"])
        assert result == "valid"

    def test_empty_list(self):
        assert digest_value_to_text([]) == ""

    def test_dict_with_unicode(self):
        result = digest_value_to_text({"名前": "太郎"})
        assert "太郎" in result

    def test_restores_latex_escape_collisions(self):
        # Poisoned stock digests carry JSON-escape collisions (`\forall` →
        # `\x0c orall`); this chokepoint heals them before they reach the
        # appendix writer, the facet index, or the embedder.
        assert (
            digest_value_to_text("$a \\pmod n, \x0corall a$ 與 $\x08inom{n}{k}$")
            == "$a \\pmod n, \\forall a$ 與 $\\binom{n}{k}$"
        )

    def test_restores_collisions_inside_lists(self):
        assert digest_value_to_text(["\x0crac{1}{2}", "\x1bll_p 空間"]) == (
            "\\frac{1}{2}; \\ell_p 空間"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
