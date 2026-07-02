from maintenance.retrieval_bench import _matches_expected, run_retrieval_bench


class FakeRAG:
    def __init__(self, results_by_query):
        self.results_by_query = results_by_query
        self.calls = []

    def query_notes(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.results_by_query.get(query, [])


def result(title, source="doc.md", cid="chunk_1"):
    return {
        "id": cid,
        "text": "body",
        "metadata": {"title": title, "source": source},
    }


def test_retrieval_bench_passes_and_writes_log(tmp_path):
    bench = tmp_path / "bench.yml"
    log = tmp_path / "maintenance.log.md"
    bench.write_text(
        """
queries:
  - query: alpha
    expected_top_1: Alpha Doc
  - query: beta
    expected_top_k: [Beta Doc]
    top_k: 2
""",
        encoding="utf-8",
    )
    rag = FakeRAG(
        {
            "alpha": [result("Alpha Doc")],
            "beta": [result("Other"), result("Beta Doc", cid="chunk_2")],
        }
    )

    out = run_retrieval_bench(
        rag, bench_path=bench, auto_path=None, log_path=log, min_pass_rate=1.0
    )

    assert out.status == "passed"
    assert out.passed == 2
    assert out.total == 2
    assert rag.calls[1][1]["top_k"] == 2
    text = log.read_text(encoding="utf-8")
    assert "Retrieval Bench" in text
    assert "| PASS | `alpha`" in text
    assert "| PASS | `beta`" in text


def test_retrieval_bench_fails_below_threshold(tmp_path):
    bench = tmp_path / "bench.yml"
    log = tmp_path / "maintenance.log.md"
    bench.write_text(
        """
- query: alpha
  expected_top_1: Missing Doc
""",
        encoding="utf-8",
    )
    rag = FakeRAG({"alpha": [result("Alpha Doc")]})

    out = run_retrieval_bench(
        rag, bench_path=bench, auto_path=None, log_path=log, min_pass_rate=0.8
    )

    assert out.status == "failed"
    assert out.pass_rate == 0.0
    assert "| FAIL | `alpha`" in log.read_text(encoding="utf-8")


def test_matches_expected_is_document_level_not_chunk_level():
    # Header promises no alert on "harmless Part/Synthesis swaps": retrieving
    # a different chunk of the *right* document must count as a hit.
    got_part1 = result("NIST.AI.600-1 (Part 1)")
    expected = ["NIST.AI.600-1 (Part 15)", "NIST.AI.600-1 (Synthesis)"]
    assert _matches_expected(got_part1, expected)

    # Stitched/Synthesis swaps collapse to the same base too.
    assert _matches_expected(result("NIST.AI.600-1 (Stitched)"), expected)


def test_matches_expected_still_fails_on_different_document():
    # Real drift — a different document — must still fail.
    assert not _matches_expected(
        result("G.H. Hardy A Course of Pure Mathematics (Part 3)"),
        ["TRENCH_REAL_ANALYSIS (Part 105)", "TRENCH_REAL_ANALYSIS (Stitched)"],
    )
    # A trailing language tag is NOT a chunk suffix; editions stay distinct.
    assert not _matches_expected(
        result("Siddhartha(DE) (Part 5)"),
        ["Siddhartha(EN) (Part 15)"],
    )


def test_retrieval_bench_skips_without_cases(tmp_path):
    out = run_retrieval_bench(
        FakeRAG({}),
        bench_path=tmp_path / "missing.yml",
        auto_path=None,
        log_path=tmp_path / "maintenance.log.md",
    )

    assert out.status == "skipped"
    assert out.total == 0
