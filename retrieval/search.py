import logging
from elasticsearch import Elasticsearch

from storage.es_client import get_client, INDEX
from embedding.embedder import embed_texts

logger = logging.getLogger("search")

_RRF_K        = 60     # RRF rank constant
MIN_RRF_SCORE = 0.020  # minimum score to return a result


def search(
    query: str,
    top_k: int = 5,
    es: Elasticsearch = None,
) -> list[dict]:
    """
    Hybrid search: BM25 full-text + kNN vector search, merged via score-based fusion.

    Final score = 0.4 * norm_bm25 + 0.6 * adjusted_knn

    - kNN uses dot_product on normalised vectors → ES score in [0, 1] where
      0.5 = orthogonal (no match), 1.0 = identical. Shifted and scaled to [0, 1]
      so unrelated docs score ~0 instead of ~0.5.
    - BM25 score normalised by max score in the result window → [0, 1].
    - Results below RELEVANCE_THRESHOLD are dropped entirely.
    """
    if es is None:
        es = get_client()

    query_vector = embed_texts([query])[0]
    window       = top_k * 10
    source       = {"excludes": ["embedding"]}

    # BM25
    bm25_resp = es.search(index=INDEX, body={
        "query": {"match": {"text": query}},
        "size":  window,
        "_source": source,
    })

    # kNN
    knn_resp = es.search(index=INDEX, body={
        "knn": {
            "field":          "embedding",
            "query_vector":   query_vector,
            "num_candidates": window,
        },
        "size":    window,
        "_source": source,
    })

    bm25_hits = bm25_resp["hits"]["hits"]
    knn_hits  = knn_resp["hits"]["hits"]

    # Quality gate: ES dot_product score = (1 + cosine) / 2.
    # Score < 0.65 means cosine < 0.3 — no meaningful semantic match in corpus.
    # Drop those kNN hits before RRF so irrelevant-only queries return nothing.
    knn_hits = [h for h in knn_hits if h["_score"] >= 0.65]

    if not bm25_hits and not knn_hits:
        logger.info(f"Query '{query[:50]}' → 0 results (no relevant match)")
        return []

    # Build rank maps (0-based) over the remaining hits
    bm25_rank: dict[str, int] = {h["_id"]: rank for rank, h in enumerate(bm25_hits)}
    knn_rank:  dict[str, int] = {h["_id"]: rank for rank, h in enumerate(knn_hits)}

    docs_map: dict[str, dict] = {}
    for h in bm25_hits:
        docs_map[h["_id"]] = h["_source"]
    for h in knn_hits:
        docs_map[h["_id"]] = h["_source"]

    # Full RRF over union — docs in only one list still get a partial score
    scores: dict[str, float] = {}
    for doc_id in docs_map:
        score = 0.0
        if doc_id in bm25_rank:
            score += 1 / (_RRF_K + bm25_rank[doc_id] + 1)
        if doc_id in knn_rank:
            score += 1 / (_RRF_K + knn_rank[doc_id] + 1)
        scores[doc_id] = score

    ranked  = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    results = []
    for doc_id, score in ranked:
        if score < MIN_RRF_SCORE:
            break   # sorted descending — nothing after this passes either
        result           = docs_map[doc_id]
        result["_score"] = round(score, 6)
        results.append(result)

    logger.info(f"Query '{query[:50]}' → {len(results)} results")
    return results
