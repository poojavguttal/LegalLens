import logging
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

from storage.es_client import get_client, INDEX

logger = logging.getLogger("indexer")

# ── Index mapping ──────────────────────────────────────────────────────────────

MAPPING = {
    "mappings": {
        "properties": {
            # --- shared fields ---
            "chunk_index":   {"type": "integer"},
            "chunk_type":    {"type": "keyword"},
            "doc_type":      {"type": "keyword"},
            "text":          {"type": "text"},
            "token_count":   {"type": "integer"},
            "filename":      {"type": "keyword"},
            "file_path":     {"type": "keyword"},

            # --- vector ---
            "embedding": {
                "type":       "dense_vector",
                "dims":       768,
                "index":      True,
                "similarity": "dot_product",   # works because embeddings are normalised
            },

            # --- PDF fields ---
            "section_header": {"type": "text"},
            "page_number":    {"type": "integer"},
            "document_date":  {"type": "keyword"},
            "content_hash":   {"type": "keyword"},

            # --- email fields ---
            "subject":        {"type": "text"},
            "sender":         {"type": "keyword"},
            "recipients":     {"type": "keyword"},
            "cc":             {"type": "keyword"},
            "bcc":            {"type": "keyword"},
            "date":           {"type": "keyword"},
            "message_id":     {"type": "keyword"},
            "thread_id":      {"type": "keyword"},
            "thread_length":  {"type": "integer"},
            "parent_chunk_index":   {"type": "integer"},
            "fragment_index":       {"type": "integer"},

            # --- JSON fields ---
            "record_index":          {"type": "integer"},
            "record_fragment_index": {"type": "integer"},
        }
    },
    "settings": {
        "number_of_shards":   1,
        "number_of_replicas": 0,    # single-node local setup
    }
}


# ── Index management ───────────────────────────────────────────────────────────

def create_index(es: Elasticsearch, recreate: bool = False) -> None:
    """Create the legallens index. If recreate=True, drop and recreate."""
    if es.indices.exists(index=INDEX):
        if recreate:
            es.indices.delete(index=INDEX)
            logger.info(f"Deleted existing index: {INDEX}")
        else:
            logger.info(f"Index already exists: {INDEX}")
            return
    es.indices.create(index=INDEX, body=MAPPING)
    logger.info(f"Created index: {INDEX}")


# ── Bulk indexer ───────────────────────────────────────────────────────────────

def index_docs(docs: list[dict], es: Elasticsearch = None) -> int:
    """
    Bulk index a list of embedded chunk dicts into ES.
    Returns number of successfully indexed docs.
    """
    if not docs:
        return 0

    if es is None:
        es = get_client()

    actions = [
        {
            "_index": INDEX,
            "_id":    f"{doc.get('filename', 'doc')}_{doc.get('chunk_index', i)}",
            "_source": doc,
        }
        for i, doc in enumerate(docs)
    ]

    success, errors = bulk(es, actions, raise_on_error=False)
    if errors:
        logger.error(f"{len(errors)} indexing errors: {errors[:3]}")
    logger.info(f"Indexed {success}/{len(docs)} docs into '{INDEX}'")
    return success
