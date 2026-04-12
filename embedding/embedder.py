import logging
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("embedder")

_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {_MODEL_NAME}")
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed_texts(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """
    Embed a list of strings. Returns a list of 768-dim vectors.
    Model is loaded once and reused across calls.
    """
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=len(texts) > 100,
        convert_to_numpy=True,
        normalize_embeddings=True,   # cosine similarity via dot product
    )
    return vectors.tolist()


def embed_chunks(chunks: list, batch_size: int = 64) -> list[dict]:
    """
    Embed a list of chunk dataclass objects (Chunk, EmailChunk, JsonChunk).
    Returns list of dicts ready for ES indexing — all chunk fields + 'embedding' vector.
    """
    import dataclasses

    if not chunks:
        return []

    texts = [c.text for c in chunks]
    vectors = embed_texts(texts, batch_size=batch_size)

    docs = []
    for chunk, vector in zip(chunks, vectors):
        doc = dataclasses.asdict(chunk)
        # Drop sentinel -1 fields
        doc = {k: v for k, v in doc.items() if v != -1}
        doc["embedding"] = vector
        docs.append(doc)

    logger.info(f"Embedded {len(docs)} chunks")
    return docs
