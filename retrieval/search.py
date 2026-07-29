import logging
from elasticsearch import Elasticsearch

from storage.es_client import get_client, INDEX
from embedding.embedder import embed_texts
from retrieval.confidence import cosine_from_es_score, score_confidence

logger = logging.getLogger("search")

_RRF_K        = 60     # RRF rank constant
MIN_RRF_SCORE = 0.020  # minimum score to return a result

# Map NLI doc_type labels → ES filter clauses.
# Only the PDF chunker sets doc_type; EmailChunk and JsonChunk don't have the
# field, so those two filter on chunk_type instead. Filtering emails on
# doc_type matches nothing and silently empties the result set.
_DOC_TYPE_FILTERS = {
    "pdf":        {"term":  {"doc_type": "pdf"}},
    "email":      {"terms": {"chunk_type": ["email", "email_fragment"]}},
    "compliance": {"term":  {"chunk_type": "json_provision"}},
}


def _build_es_filters(filters: dict | None) -> list[dict]:
    """Convert NLI filter dict → list of ES query DSL filter clauses."""
    if not filters:
        return []
    clauses = []
    doc_type = filters.get("doc_type")
    if doc_type and doc_type in _DOC_TYPE_FILTERS:
        clauses.append(_DOC_TYPE_FILTERS[doc_type])
    if filters.get("date_from"):
        clauses.append({"range": {"document_date": {"gte": filters["date_from"]}}})
    if filters.get("date_to"):
        clauses.append({"range": {"document_date": {"lte": filters["date_to"]}}})
    return clauses


def search(
    query: str,
    top_k: int = 5,
    es: Elasticsearch = None,
    filters: dict | None = None,
) -> list[dict]:
    """
    Hybrid search: BM25 full-text + kNN vector search, merged via RRF.

    Optional `filters` dict (from NLI query_processor) supports:
      doc_type  → "pdf" | "email" | "compliance"
      date_from → "YYYY-MM-DD"
      date_to   → "YYYY-MM-DD"

    Results below MIN_RRF_SCORE are dropped (relevance gate).
    """
    if es is None:
        es = get_client()

    query_vector   = embed_texts([query])[0]
    window         = top_k * 10
    source         = {"excludes": ["embedding"]}
    filter_clauses = _build_es_filters(filters)

    # BM25
    if filter_clauses:
        bm25_query = {"bool": {"must": {"match": {"text": query}}, "filter": filter_clauses}}
    else:
        bm25_query = {"match": {"text": query}}

    bm25_resp = es.search(index=INDEX, body={
        "query":   bm25_query,
        "size":    window,
        "_source": source,
    })

    # kNN
    knn_body: dict = {
        "knn": {
            "field":          "embedding",
            "query_vector":   query_vector,
            "num_candidates": window,
        },
        "size":    window,
        "_source": source,
    }
    if filter_clauses:
        knn_filter = filter_clauses[0] if len(filter_clauses) == 1 else {"bool": {"filter": filter_clauses}}
        knn_body["knn"]["filter"] = knn_filter

    knn_resp = es.search(index=INDEX, body=knn_body)

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
    # Keep the raw similarity — RRF discards it, and confidence needs it back
    cosines:   dict[str, float] = {
        h["_id"]: cosine_from_es_score(h["_score"]) for h in knn_hits
    }

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
        # RRF orders the results; confidence explains them. See retrieval/confidence.py
        result["_signals"] = {
            "cosine":    cosines.get(doc_id),
            "bm25_rank": bm25_rank.get(doc_id),
            "knn_rank":  knn_rank.get(doc_id),
        }
        result["_confidence"] = score_confidence(
            cosine    = cosines.get(doc_id),
            bm25_rank = bm25_rank.get(doc_id),
        )
        results.append(result)

    logger.info(f"Query '{query[:50]}' → {len(results)} results")
    return results
