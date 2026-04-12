import logging
import re
from dataclasses import dataclass
from pathlib import Path

from chunking.pdf_chunker import _tokens
from ingestion.email_ingester import ingest_email_file, ingest_emails_directory, EmailDocument

logger = logging.getLogger("email_chunker")


@dataclass
class EmailChunk:
    chunk_index: int
    chunk_type: str             # "email" or "email_fragment"
    text: str
    token_count: int
    filename: str
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    cc: str = ""
    bcc: str = ""
    date: str = ""
    message_id: str = ""
    thread_id: str = ""
    thread_length: int = 0
    parent_chunk_index: int = -1  # set on email_fragment — all fragments point to chunk_index_start
    fragment_index: int = -1      # 1-based position within the split; -1 = not a fragment


# ── Split only at paragraph boundaries ────────────────────────────────────────

def _split_paragraphs(body: str, token_budget: int) -> list[str]:
    """
    Greedily fill fragments up to token_budget at paragraph boundaries.
    Only called when the full body exceeds token_budget.
    """
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    fragments, current, current_tok = [], [], 0

    for para in paragraphs:
        para_tok = _tokens(para)
        if current and current_tok + para_tok > token_budget:
            fragments.append("\n\n".join(current))
            current, current_tok = [para], para_tok
        else:
            current.append(para)
            current_tok += para_tok

    if current:
        fragments.append("\n\n".join(current))

    return fragments


# ── Main chunker ───────────────────────────────────────────────────────────────

def chunk_email_document(
    doc: EmailDocument,
    chunk_index_start: int = 0,
    token_budget: int = 512,
) -> list[EmailChunk]:
    """
    Chunk a single EmailDocument.
    Keeps each email as one chunk. Only splits at paragraph boundaries if body > token_budget.
    """
    body = doc.body

    if not body or _tokens(body) < 5:
        logger.warning(f"Skipping {doc.filename} — empty or too short")
        return []

    if _tokens(body) <= token_budget:
        fragments = [body]
    else:
        fragments = _split_paragraphs(body, token_budget)

    chunk_index  = chunk_index_start
    is_split     = len(fragments) > 1
    chunks       = []

    for i, fragment in enumerate(fragments):
        chunk_type = "email_fragment" if is_split else "email"

        chunk = EmailChunk(
            chunk_index=chunk_index,
            chunk_type=chunk_type,
            text=fragment,
            token_count=_tokens(fragment),
            filename=doc.filename,
            subject=doc.subject,
            sender=doc.sender,
            recipients=doc.recipients,
            cc=doc.cc,
            bcc=doc.bcc,
            date=doc.date,
            message_id=doc.message_id,
            thread_id=doc.thread_id,
            thread_length=doc.thread_length,
        )
        if is_split:
            chunk.parent_chunk_index = chunk_index_start
            chunk.fragment_index     = i + 1
        chunks.append(chunk)
        chunk_index += 1

    logger.info(
        f"{doc.filename} → {len(fragments)} chunk(s) | "
        f"thread_length={doc.thread_length} | from={doc.sender}"
    )
    return chunks


def chunk_emails_directory(
    directory: str,
    token_budget: int = 512,
) -> list[EmailChunk]:
    """Ingest and chunk all .txt email files in a directory."""
    docs = ingest_emails_directory(directory)
    all_chunks = []
    for doc in docs:
        chunks = chunk_email_document(
            doc,
            chunk_index_start=len(all_chunks),
            token_budget=token_budget,
        )
        all_chunks.extend(chunks)

    logger.info(f"Total: {len(all_chunks)} chunks from {len(docs)} email files")
    return all_chunks
