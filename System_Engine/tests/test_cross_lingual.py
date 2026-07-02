from services.cross_lingual import detect_lang, expand_queries


class TestDetectLang:
    def test_pure_chinese_is_zh(self):
        assert detect_lang("弱解的存在性是怎麼證明的") == "zh"

    def test_pure_english_is_en(self):
        assert detect_lang("how is the existence of weak solutions proven") == "en"

    def test_mixed_with_meaningful_cjk_is_zh(self):
        assert detect_lang("用 Lax-Milgram 證明弱解存在") == "zh"

    def test_empty_defaults_en(self):
        assert detect_lang("") == "en"

    def test_codey_english_with_no_cjk_is_en(self):
        assert detect_lang("def query_notes(self): return rrf_merge([])") == "en"


class TestExpandQueries:
    def _translator(self, mapping):
        calls = []

        def fn(text, langs):
            calls.append((text, tuple(langs)))
            return mapping

        fn.calls = calls
        return fn

    def test_zh_query_translates_to_en_only(self):
        # A Chinese query should be translated into en (not zh — its own lang).
        tr = self._translator({"en": "existence proof of weak solutions"})
        out = expand_queries("弱解的存在性", tr, ["en", "zh"])
        assert out == ["existence proof of weak solutions"]
        # Only the non-source language was requested.
        assert tr.calls[0][1] == ("en",)

    def test_en_query_translates_to_zh_only(self):
        tr = self._translator({"zh": "弱解存在性證明"})
        out = expand_queries("weak solution existence", tr, ["en", "zh"])
        assert out == ["弱解存在性證明"]
        assert tr.calls[0][1] == ("zh",)

    def test_no_translator_returns_empty(self):
        assert expand_queries("anything", None, ["en", "zh"]) == []

    def test_translator_failure_fails_open(self):
        def boom(text, langs):
            raise RuntimeError("LLM down")

        assert expand_queries("弱解", boom, ["en"]) == []

    def test_variant_equal_to_original_is_dropped(self):
        tr = self._translator({"en": "  弱解  "})  # echoes original after strip
        assert expand_queries("弱解", tr, ["en"]) == []

    def test_blank_translation_skipped(self):
        tr = self._translator({"en": "   "})
        assert expand_queries("弱解", tr, ["en"]) == []

    def test_no_other_target_lang_returns_empty(self):
        # Query is en and the only target is en → nothing to translate into.
        tr = self._translator({"en": "x"})
        assert expand_queries("hello world", tr, ["en"]) == []
