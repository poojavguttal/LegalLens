import logging
import ijson
from dataclasses import dataclass, field

from chunking.pdf_chunker import _tokens

logger = logging.getLogger("json_chunker")


@dataclass
class JsonChunk:
    chunk_index: int
    chunk_type: str = "json_provision"
    text: str = ""                   # key: value formatted text
    token_count: int = 0
    record_index: int = -1           # which record in the JSONL file
    record_fragment_index: int = -1  # which fragment of that record (-1 = not split)
    # Citation metadata — distinct values across every record merged into this chunk
    source_documents: list[str] = field(default_factory=list)
    provision_labels: list[str] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _distinct(values: list[str]) -> list[str]:
    """Dedupe while preserving order."""
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _record_source(record: dict) -> str:
    """The originating filing path for a LEDGAR record ('' if absent)."""
    return str(record.get("source") or "").strip()


def _record_labels(record: dict) -> list[str]:
    """Provision labels for a LEDGAR record — normalised to a list of strings."""
    labels = record.get("label") or []
    if isinstance(labels, str):
        labels = [labels]
    return [str(x).strip() for x in labels if str(x).strip()]


def _split_by_words(text: str, token_budget: int) -> list[str]:
    """Fallback: split a single oversized string at word boundaries."""
    words = text.split()
    chunks, current = [], []
    for word in words:
        current.append(word)
        if _tokens(" ".join(current)) > token_budget:
            current.pop()
            if current:
                chunks.append(" ".join(current))
            current = [word]
    if current:
        chunks.append(" ".join(current))
    return chunks


def _format_record(record: dict) -> str:
    """Format a whole record as key: value lines."""
    lines = []
    for k, v in record.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _kv_blocks(record: dict, token_budget: int) -> list[str]:
    """
    Split record at key boundaries. Each 'key: value' is one block.
    If a single key-value block exceeds token_budget, fall back to word split of that value.
    """
    blocks = []
    for k, v in record.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        kv = f"{k}: {v}"
        if _tokens(kv) > token_budget:
            prefix_tok = _tokens(f"{k}: ")
            fragments  = _split_by_words(str(v), token_budget - prefix_tok)
            for i, frag in enumerate(fragments):
                blocks.append(f"{k}: {frag}")
        else:
            blocks.append(kv)
    return blocks


def _greedy_merge(blocks: list[str], token_budget: int) -> list[str]:
    """Greedy merge a list of blocks up to token_budget."""
    merged, current, current_tok = [], [], 0
    for block in blocks:
        tok = _tokens(block)
        if current and current_tok + tok > token_budget:
            merged.append("\n\n".join(current))
            current, current_tok = [block], tok
        else:
            current.append(block)
            current_tok += tok
    if current:
        merged.append("\n\n".join(current))
    return merged



# ── Main chunker ───────────────────────────────────────────────────────────────

def chunk_ledgar_file(
    file_path: str,
    token_budget: int = 512,
    max_records: int = None,
) -> list[JsonChunk]:
    """
    Stream a LEDGAR .jsonl file and chunk using RSM-style greedy merge.

    - Normal record (≤ 512 tokens): provision text as one block, greedy merge with adjacent.
    - Oversized record (> 512 tokens): split at key boundaries, greedy merge key-value blocks.
    - Single key-value still oversized: fall back to word-level split.
    - Split records carry record_index and record_fragment_index for traceability.
    """
    chunks        = []
    chunk_index   = 0
    pending_text  : list[str] = []
    pending_tok   = 0
    pending_records: list[int] = []
    pending_sources: list[str] = []
    pending_labels : list[str] = []

    def flush():
        nonlocal chunk_index, pending_text, pending_tok, pending_records
        nonlocal pending_sources, pending_labels
        if not pending_text:
            return
        chunks.append(JsonChunk(
            chunk_index      = chunk_index,
            text             = "\n\n".join(pending_text),
            token_count      = pending_tok,
            record_index     = pending_records[0] if len(pending_records) == 1 else -1,
            source_documents = _distinct(pending_sources),
            provision_labels = _distinct(pending_labels),
        ))
        chunk_index     += 1
        pending_text     = []
        pending_tok      = 0
        pending_records  = []
        pending_sources  = []
        pending_labels   = []

    records_seen = 0
    with open(file_path, "rb") as fh:
        for record in ijson.items(fh, "", multiple_values=True):
            if not record.get("provision", "").strip():
                records_seen += 1
                continue

            kv_text = _format_record(record)
            tok     = _tokens(kv_text)
            source  = _record_source(record)
            labels  = _record_labels(record)

            if tok > token_budget:
                flush()
                blocks   = _kv_blocks(record, token_budget)
                merged   = _greedy_merge(blocks, token_budget)
                for frag_i, frag_text in enumerate(merged):
                    chunks.append(JsonChunk(
                        chunk_index           = chunk_index,
                        text                  = frag_text,
                        token_count           = _tokens(frag_text),
                        record_index          = records_seen,
                        record_fragment_index = frag_i + 1,
                        source_documents      = [source] if source else [],
                        provision_labels      = labels,
                    ))
                    chunk_index += 1
            else:
                if pending_tok + tok > token_budget:
                    flush()
                pending_text.append(kv_text)
                pending_tok += tok
                pending_records.append(records_seen)
                if source:
                    pending_sources.append(source)
                pending_labels.extend(labels)

            records_seen += 1
            if max_records and records_seen >= max_records:
                break

    flush()
    logger.info(f"Chunked {records_seen} provisions → {len(chunks)} chunks")
    return chunks
