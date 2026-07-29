"""
Collapse chunk-level search hits into one entry per source document.

Retrieval scores chunks, and a single document frequently contributes several of
them — which surfaces as the same file cited two or three times over. Grouping
merges those into one cited source. Nothing is discarded: every excerpt still
reaches the synthesizer under the group's citation number, and each chunk's page
is kept so the PDF viewer can highlight all of them.
"""
import logging

logger = logging.getLogger("grouping")

EXCERPT_SEPARATOR = "\n\n[…]\n\n"


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def _document_key(result: dict, index: int):
    """
    Stable identity of the document a chunk came from.

    PDFs and emails are identified by filename; LEDGAR provisions by the filing
    they were extracted from. A chunk with neither is ungroupable and keyed by
    its own position so it never merges with anything else.
    """
    if result.get("filename"):
        return ("file", result["filename"])
    sources = _as_list(result.get("source_documents"))
    if sources:
        return ("source", sources[0])
    return ("chunk", index)


def group_by_document(results: list[dict]) -> list[dict]:
    """
    Merge chunks sharing a source document, preserving relevance order.

    Each returned dict carries the best-ranked chunk's metadata plus:
      chunks       — every chunk from that document, best-ranked first
      pages        — sorted distinct page numbers those chunks came from
      text         — all excerpts joined, for answer synthesis
      _score       — the best chunk's score
      _confidence  — the best chunk's confidence (a document is as good as its
                     strongest match; weaker excerpts can't dilute it)
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []

    for index, result in enumerate(results):
        key = _document_key(result, index)
        if key in groups:
            groups[key]["chunks"].append(result)
        else:
            # First hit wins the metadata — results arrive best-first, so the
            # group's page_number is the one worth jumping to.
            merged = dict(result)
            merged["chunks"] = [result]
            groups[key] = merged
            order.append(key)

    merged_results = []
    for key in order:
        group = groups[key]
        chunks = group["chunks"]
        group["pages"] = sorted({
            int(c["page_number"]) for c in chunks if c.get("page_number")
        })
        group["text"] = EXCERPT_SEPARATOR.join(c["text"] for c in chunks)
        group["_score"] = max(c.get("_score", 0) for c in chunks)
        group["_confidence"] = max(c.get("_confidence", 0) for c in chunks)
        merged_results.append(group)

    if len(merged_results) < len(results):
        logger.info(f"Grouped {len(results)} chunks → {len(merged_results)} documents")
    return merged_results
