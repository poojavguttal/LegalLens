"""
Retrieval layer tests — accuracy and speed.

Unit tests mock the ES client (no ES required).
Integration tests (marked with @pytest.mark.integration) require ES + indexed data.

Run unit tests only:   python -m pytest tests/test_retrieval.py -v -m "not integration"
Run all (needs ES):    python -m pytest tests/test_retrieval.py -v
"""
import time
from dataclasses import fields

import pytest
from unittest.mock import MagicMock, patch

from chunking.email_chunker import EmailChunk
from chunking.json_chunker import JsonChunk
from chunking.pdf_chunker import Chunk
from retrieval.search import search, _build_es_filters, _DOC_TYPE_FILTERS, _RRF_K, MIN_RRF_SCORE


# ── Mock ES hit factory ────────────────────────────────────────────────────────

def _hit(doc_id: str, bm25_score: float = 1.0, source: dict = None) -> dict:
    return {
        "_id":     doc_id,
        "_score":  bm25_score,
        "_source": source or {"text": f"text for {doc_id}", "chunk_type": "section", "filename": "test.pdf"},
    }


def _mock_es(bm25_hits: list, knn_hits: list) -> MagicMock:
    """Return a mock ES client that returns given hits for BM25 and kNN queries."""
    es = MagicMock()
    responses = [
        {"hits": {"hits": bm25_hits}},
        {"hits": {"hits": knn_hits}},
    ]
    es.search.side_effect = responses
    return es


# ══════════════════════════════════════════════════════════════════════════════
# RRF Score Accuracy
# ══════════════════════════════════════════════════════════════════════════════

class TestRRFScoring:

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_doc_in_both_lists_scores_higher(self, _):
        """A doc in both BM25 and kNN should outscore a doc in only one list."""
        bm25_hits = [_hit("doc_both"), _hit("doc_bm25_only")]
        knn_hits  = [_hit("doc_both", 0.9), _hit("doc_knn_only", 0.9)]
        es = _mock_es(bm25_hits, knn_hits)

        results = search("force majeure", es=es)
        ids     = [r.get("text", "").split()[-1] for r in results]  # rough check

        scores = {r["_score"] for r in results}
        # doc_both should have a higher RRF score than any single-list doc
        both_score = next(
            r["_score"] for r in results
            if "doc_both" in str(r)
        )
        single_scores = [
            r["_score"] for r in results
            if "doc_both" not in str(r)
        ]
        if single_scores:
            assert both_score >= max(single_scores)

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_rrf_formula_correct(self, _):
        """Manually verify RRF score for a doc at rank 0 in both lists."""
        bm25_hits = [_hit("doc_a")]
        knn_hits  = [_hit("doc_a", 0.9)]
        es = _mock_es(bm25_hits, knn_hits)

        results = search("contract termination", es=es)
        assert len(results) == 1
        expected = round(1 / (_RRF_K + 1) + 1 / (_RRF_K + 1), 6)
        assert results[0]["_score"] == expected

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_results_sorted_by_score_descending(self, _):
        """Results must be returned in descending score order."""
        bm25_hits = [_hit("doc_a"), _hit("doc_b"), _hit("doc_c")]
        knn_hits  = [_hit("doc_c", 0.9), _hit("doc_b", 0.85), _hit("doc_a", 0.8)]
        es = _mock_es(bm25_hits, knn_hits)

        results = search("legal obligations", es=es)
        scores  = [r["_score"] for r in results]
        assert scores == sorted(scores, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# Relevance Gate (Minimum Score Threshold)
# ══════════════════════════════════════════════════════════════════════════════

class TestRelevanceGate:

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_no_bm25_hits_returns_empty(self, _):
        """When BM25 finds nothing, return no results (off-topic query)."""
        bm25_hits = []
        knn_hits  = [_hit("doc_a", 0.9), _hit("doc_b", 0.85)]
        es = _mock_es(bm25_hits, knn_hits)

        results = search("what is the weather today", es=es)
        assert results == [], "Expected 0 results when BM25 has no hits"

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_low_rrf_score_filtered_out(self, _):
        """Docs with RRF score below MIN_RRF_SCORE should be dropped."""
        # doc_a at rank 0 in BM25 only → score = 1/(60+1) ≈ 0.0164 < 0.020
        bm25_hits = [_hit("doc_a")]
        knn_hits  = [_hit("doc_b", 0.9)]   # doc_a not in kNN → partial score only
        es = _mock_es(bm25_hits, knn_hits)

        results = search("termination clause", es=es)
        # doc_a scores 1/61 ≈ 0.0164 which is below MIN_RRF_SCORE
        for r in results:
            assert r["_score"] >= MIN_RRF_SCORE

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_both_lists_empty_returns_empty(self, _):
        es = _mock_es([], [])
        results = search("anything", es=es)
        assert results == []

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_top_k_respected(self, _):
        """Never return more than top_k results."""
        hits = [_hit(f"doc_{i}") for i in range(20)]
        es   = _mock_es(hits, hits)
        results = search("contract", top_k=3, es=es)
        assert len(results) <= 3


# ══════════════════════════════════════════════════════════════════════════════
# doc_type Filters — must target fields the chunkers actually write
# ══════════════════════════════════════════════════════════════════════════════

class TestDocTypeFilters:
    """
    Only the PDF chunker sets doc_type. Filtering emails or provisions on that
    field matches nothing and silently empties the result set — the failure mode
    looks identical to "nothing was indexed".
    """

    def _filtered_fields(self, doc_type: str) -> set[str]:
        clauses = _build_es_filters({"doc_type": doc_type})
        fields  = set()
        for clause in clauses:
            for body in clause.values():        # {"term": {...}} / {"terms": {...}}
                fields.update(body.keys())
        return fields

    def test_pdf_filters_on_doc_type(self):
        assert self._filtered_fields("pdf") == {"doc_type"}

    def test_email_filters_on_chunk_type(self):
        """EmailChunk has no doc_type field."""
        assert self._filtered_fields("email") == {"chunk_type"}

    def test_compliance_filters_on_chunk_type(self):
        """JsonChunk has no doc_type field."""
        assert self._filtered_fields("compliance") == {"chunk_type"}

    def test_email_filter_covers_split_emails(self):
        """Long emails are chunked as email_fragment — both must survive."""
        clause = _DOC_TYPE_FILTERS["email"]
        assert set(clause["terms"]["chunk_type"]) == {"email", "email_fragment"}

    def test_filter_fields_exist_on_their_chunk_dataclass(self):
        """Guard against a chunker dropping a field a filter depends on."""
        assert "doc_type"   in {f.name for f in fields(Chunk)}
        assert "chunk_type" in {f.name for f in fields(EmailChunk)}
        assert "chunk_type" in {f.name for f in fields(JsonChunk)}

    def test_unknown_doc_type_is_ignored(self):
        assert _build_es_filters({"doc_type": "memo"}) == []

    def test_no_filters_when_empty(self):
        assert _build_es_filters(None) == []
        assert _build_es_filters({}) == []


# ══════════════════════════════════════════════════════════════════════════════
# Speed Tests (Unit — mocked ES, measuring Python logic only)
# ══════════════════════════════════════════════════════════════════════════════

class TestSearchSpeed:

    @patch("retrieval.search.embed_texts", return_value=[[0.1] * 768])
    def test_rrf_merge_speed(self, _):
        """RRF Python merging of 50 candidates should complete in < 50ms."""
        hits = [_hit(f"doc_{i}") for i in range(50)]
        es   = _mock_es(hits, hits)

        t0      = time.time()
        results = search("legal", top_k=5, es=es)
        elapsed = time.time() - t0

        print(f"\n  RRF merge time (50 candidates): {elapsed*1000:.2f}ms")
        assert elapsed < 0.05, f"RRF merge too slow: {elapsed:.3f}s"


# ══════════════════════════════════════════════════════════════════════════════
# Integration Tests — require ES running with indexed data
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestSearchIntegration:
    """
    Require: ES running on localhost:9200 with legallens index populated.
    Run with: python -m pytest tests/test_retrieval.py -v -m integration
    """

    def test_relevant_query_returns_results(self):
        """A legal query should return results from the indexed corpus."""
        t0      = time.time()
        results = search("Dr. Mahendra Amin was accused for what reason", top_k=5)
        elapsed = time.time() - t0
        print(f"\n  Search latency: {elapsed*1000:.0f}ms")
        assert len(results) > 0, "Expected results for a relevant legal query"

    def test_irrelevant_query_returns_no_results(self):
        """An off-topic query should return no results."""
        results = search("Do you think humans are more driven by logic or emotion?", top_k=5)
        assert results == [], "Expected 0 results for off-topic query"

    def test_scores_above_threshold(self):
        """All returned results must meet the minimum score threshold."""
        results = search("payment obligations thirty days invoice", top_k=5)
        for r in results:
            assert r["_score"] >= MIN_RRF_SCORE

    def test_results_have_required_fields(self):
        """Every result must have text and chunk_type fields."""
        results = search("Dr. Mahendra Amin was accused for what reason", top_k=5)
        for r in results:
            assert "text"       in r, "Missing 'text' field"
            assert "chunk_type" in r, "Missing 'chunk_type' field"
            assert "_score"     in r, "Missing '_score' field"

    def test_search_speed_under_2_seconds(self):
        """End-to-end search (embed + BM25 + kNN + RRF) must complete < 2s."""
        t0      = time.time()
        results = search("In Arotin case what did the  Appellants request the  relief from the trial?", top_k=5)
        elapsed = time.time() - t0
        print(f"\n  End-to-end search latency: {elapsed*1000:.0f}ms")
        assert elapsed < 2.0, f"Search too slow: {elapsed:.2f}s"

    def test_pdf_chunks_returned_for_contract_query(self):
        """Contract-specific query should return PDF chunks."""
        results = search("In Arotin case what did the  Appellants request the  relief from the trial?", top_k=5)
        if results:
            types = [r.get("chunk_type") for r in results]
            assert any(t in ("section", "table") for t in types)

    def test_email_chunks_returned_for_email_query(self):
        """Email-specific query should return email chunks."""
        results = search("RE: EWEB Schedule to the Master Agreement", top_k=5)
        if results:
            types = [r.get("chunk_type") for r in results]
            assert any(t == "email" for t in types)
