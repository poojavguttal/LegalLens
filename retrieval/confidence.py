"""
Turn raw retrieval signals into a 0–1 confidence a person can read.

The RRF score used for *ranking* is not a confidence. It is a sum of reciprocal
ranks, so a chunk that tops both retrievers scores 2/(K+1) ≈ 0.033 — displaying
that as a percentage reads as "3% confident" for a perfect hit. RRF is also
purely ordinal: it knows chunk A outranked chunk B, and nothing about how well
either one actually matched the query.

Confidence is therefore rebuilt from the two underlying signals:

  semantic   cosine similarity between query and chunk embedding, calibrated
             against the same 0.30 floor the kNN relevance gate already uses —
             below it search.py treats a hit as noise, so it is a principled 0%.
  lexical    BM25 rank — position, not raw score, because BM25 scores are
             unbounded and not comparable across queries. It decays
             reciprocally, the same shape RRF itself uses.

They combine as a noisy-OR rather than a weighted sum, because they are
independent kinds of evidence and either one alone can be conclusive:

  - "3 Allen Center" is a proper noun. BM25 nails it; the embedding model
    scores it barely above the noise floor. A weighted sum lets a semantically
    fluffier document outrank the correct one.
  - A paraphrased legal question has no distinctive keywords. The embedding
    finds it; BM25 ranks it low or misses it entirely.

Each signal has a reliability — how confident a perfect match on that signal
alone should make you — and corroboration falls out of the noisy-OR without a
separate bonus term. These are judgement calls, not a calibrated probability
model; tune them here, nothing else reads these constants.
"""

COSINE_FLOOR = 0.30   # kNN relevance gate in search.py: below this is noise
COSINE_CEIL  = 0.80   # near-verbatim overlap for all-mpnet-base-v2

# Ranks at which the lexical signal has decayed to half. Small on purpose:
# linear decay over the candidate window makes rank 4 nearly as good as rank 1,
# which loses queries only BM25 can answer.
_LEXICAL_HALF_LIFE = 2

# Trust in a perfect match on one signal when the other is silent. Semantic
# outranks lexical: a near-verbatim embedding match is stronger evidence than
# topping BM25, which any shared rare term can do.
_SEMANTIC_RELIABILITY = 0.85
_LEXICAL_RELIABILITY  = 0.55

# Rescale so corroborated perfection reads as 100% rather than 93%.
_NORMALISER = 1.0 - (1.0 - _SEMANTIC_RELIABILITY) * (1.0 - _LEXICAL_RELIABILITY)


def cosine_from_es_score(es_score: float) -> float:
    """
    Recover cosine similarity from an ES dense_vector score.

    The index uses similarity="dot_product" over normalised embeddings, which
    Elasticsearch scores as (1 + cosine) / 2.
    """
    return 2 * es_score - 1


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _semantic_signal(cosine: float | None) -> float:
    if cosine is None:
        return 0.0
    return _clamp((cosine - COSINE_FLOOR) / (COSINE_CEIL - COSINE_FLOOR))


def _lexical_signal(rank: int | None) -> float:
    """Rank 0 (best) → 1.0, halving every _LEXICAL_HALF_LIFE positions."""
    if rank is None:
        return 0.0
    return _clamp(1.0 / (1.0 + max(0, rank) / _LEXICAL_HALF_LIFE))


def score_confidence(cosine: float | None, bm25_rank: int | None) -> float:
    """
    Combine retrieval signals into a 0–1 confidence.

    cosine     query/chunk cosine similarity, or None if the chunk never made
               the kNN list (it was gated out or fell outside num_candidates).
    bm25_rank  0-based rank in the BM25 list, or None if BM25 never matched it.
    """
    semantic = _SEMANTIC_RELIABILITY * _semantic_signal(cosine)
    lexical  = _LEXICAL_RELIABILITY  * _lexical_signal(bm25_rank)

    combined = 1.0 - (1.0 - semantic) * (1.0 - lexical)
    return round(_clamp(combined / _NORMALISER), 4)


def confidence_label(confidence: float) -> str:
    """Coarse band for the UI — keep the thresholds and the ring colours in step."""
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.50:
        return "moderate"
    return "low"
