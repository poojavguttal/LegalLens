"""
Confidence tests — the readable 0–1 score derived from raw retrieval signals.

No ES required.
"""
import pytest

from retrieval.confidence import (
    COSINE_CEIL,
    COSINE_FLOOR,
    confidence_label,
    cosine_from_es_score,
    score_confidence,
)


class TestCosineRecovery:

    def test_es_dot_product_score_maps_back_to_cosine(self):
        assert cosine_from_es_score(1.0)  == 1.0    # identical vectors
        assert cosine_from_es_score(0.5)  == 0.0    # orthogonal
        assert cosine_from_es_score(0.65) == pytest.approx(COSINE_FLOOR)

    def test_relevance_gate_matches_the_cosine_floor(self):
        """search.py drops kNN hits below 0.65 — that must be the 0% point."""
        assert score_confidence(cosine_from_es_score(0.65), None) == 0.0


class TestScoreConfidence:

    def test_perfect_hit_is_100_percent(self):
        assert score_confidence(COSINE_CEIL, 0) == 1.0

    def test_noise_floor_is_zero(self):
        assert score_confidence(COSINE_FLOOR, None) == 0.0

    def test_no_signals_is_zero(self):
        assert score_confidence(None, None) == 0.0

    def test_always_within_zero_and_one(self):
        for cosine in (-1.0, 0.0, 0.3, 0.55, 0.8, 1.0, None):
            for rank in (None, 0, 7, 49, 500):
                score = score_confidence(cosine, rank)
                assert 0.0 <= score <= 1.0, (cosine, rank, score)

    def test_monotonic_in_semantic_similarity(self):
        scores = [score_confidence(c, 5) for c in (0.35, 0.45, 0.6, 0.75)]
        assert scores == sorted(scores)
        assert scores[0] < scores[-1]

    def test_monotonic_in_lexical_rank(self):
        better = score_confidence(0.6, 0)
        worse  = score_confidence(0.6, 40)
        assert better > worse

    def test_corroboration_beats_a_single_retriever(self):
        """The same semantic match scores higher when BM25 agrees."""
        both = score_confidence(0.6, 3)
        knn_only = score_confidence(0.6, None)
        assert both > knn_only

    def test_keyword_only_match_is_capped(self):
        """No semantic support — a lexical fluke must not read as certainty."""
        assert score_confidence(None, 0) <= 0.6

    def test_strong_hit_lands_in_a_believable_band(self):
        """A genuinely good legal match should read as high, not 3% or 100%."""
        score = score_confidence(cosine=0.62, bm25_rank=1)
        assert 0.70 <= score <= 0.90
        assert confidence_label(score) == "high"

    def test_top_keyword_hit_beats_a_semantic_near_miss(self):
        """
        Regression: "3 Allen Center" over the Enron emails.

        The correct email is BM25 rank 1 with cosine barely above the floor;
        an off-topic one has better cosine at BM25 rank 4. Linear rank decay
        scored the wrong document higher.
        """
        correct   = score_confidence(cosine=0.314, bm25_rank=0)
        near_miss = score_confidence(cosine=0.420, bm25_rank=3)
        assert correct > near_miss

    def test_lexical_signal_decays_sharply(self):
        """Rank 3 must not read as almost-as-good as rank 1."""
        top    = score_confidence(None, 0)
        fourth = score_confidence(None, 3)
        assert fourth < top / 2


class TestConfidenceLabel:

    def test_bands(self):
        assert confidence_label(0.92) == "high"
        assert confidence_label(0.75) == "high"
        assert confidence_label(0.60) == "moderate"
        assert confidence_label(0.50) == "moderate"
        assert confidence_label(0.49) == "low"
        assert confidence_label(0.0)  == "low"
