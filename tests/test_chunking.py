"""
Chunking layer tests — PDF, Email, JSON.
Covers: total chunks, timing, token stats, token limits, table detection,
        flat metadata, fragment behaviour, greedy merge, KV split, word fallback.

Run: python -m pytest tests/test_chunking.py -v
"""
import json
import time
import pytest
from dataclasses import asdict
from pathlib import Path

from chunking.pdf_chunker import chunk_pdf, _tokens
from chunking.email_chunker import chunk_email_document, EmailChunk
from chunking.json_chunker import chunk_ledgar_file, _split_by_words, _kv_blocks, _greedy_merge
from ingestion.email_ingester import EmailDocument


# ── Helpers ────────────────────────────────────────────────────────────────────

TOKEN_BUDGET = 512

def _make_email(body: str, **kwargs) -> EmailDocument:
    defaults = dict(
        filename="test.txt", file_path="/tmp/test.txt", subject="Test",
        sender="a@example.com", recipients="b@example.com", cc="", bcc="",
        date="2001-01-01", message_id="<test@x.com>", thread_id="<test@x.com>",
        thread_length=0, body=body,
    )
    defaults.update(kwargs)
    return EmailDocument(**defaults)

def _word_block(n: int) -> str:
    return " ".join([f"word{i}" for i in range(n)])


# ══════════════════════════════════════════════════════════════════════════════
# PDF Chunker
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_PDF_MD = """
<!-- page 1 -->
## Introduction

This agreement is entered into by the parties as of the date set forth below.
All terms and conditions herein shall be binding upon both parties.

## Obligations

Each party shall fulfill its obligations in good faith and in accordance
with the terms specified in this contract. Failure to comply may result
in termination.

## Payment Terms

Payment shall be due within thirty (30) days of invoice receipt.
Late payments shall incur interest at the rate of 1.5% per month.

<!-- page 2 -->
## Termination

Either party may terminate this agreement upon thirty (30) days written notice.
Upon termination, all outstanding obligations shall remain enforceable.

## Governing Law

This agreement shall be governed by the laws of the State of New York.
Any disputes shall be resolved through binding arbitration.

|Party|Role|Date|
|-----|----|----|
|Acme Corp|Vendor|2026-01-01|
|Beta LLC|Client|2026-01-01|
"""


class TestPDFChunker:

    def setup_method(self):
        t0 = time.time()
        self.chunks = chunk_pdf(
            SAMPLE_PDF_MD,
            filename="test_contract.pdf",
            doc_type="pdf",
            document_date="2026-01-01",
            content_hash="abc123",
        )
        self.elapsed = time.time() - t0
        self.tokens  = [c.token_count for c in self.chunks]

    # ── Stats ──────────────────────────────────────────────────────────────

    def test_produces_chunks(self):
        assert len(self.chunks) > 0, "Expected at least one chunk"

    def test_total_chunk_count(self):
        print(f"\n  Total PDF chunks: {len(self.chunks)}")
        assert len(self.chunks) >= 2   # at least text + table

    def test_timing(self):
        print(f"\n  PDF chunking time: {self.elapsed:.4f}s")
        assert self.elapsed < 5.0, "Chunking took too long"

    # ── Token limits ───────────────────────────────────────────────────────

    def test_no_chunk_exceeds_budget(self):
        over = [c for c in self.chunks if c.token_count > TOKEN_BUDGET]
        assert over == [], f"{len(over)} chunk(s) exceed {TOKEN_BUDGET} tokens: {[c.token_count for c in over]}"

    # ── Table detection ────────────────────────────────────────────────────

    def test_table_chunk_detected(self):
        tables = [c for c in self.chunks if c.chunk_type == "table"]
        assert len(tables) >= 1, "Expected at least one table chunk"

    def test_table_preserved_as_atomic(self):
        tables = [c for c in self.chunks if c.chunk_type == "table"]
        for t in tables:
            assert "|" in t.text, "Table chunk should contain pipe characters"

    # ── Flat metadata fields ───────────────────────────────────────────────

    def test_filename_at_top_level(self):
        for c in self.chunks:
            assert c.filename == "test_contract.pdf"

    def test_doc_type_at_top_level(self):
        for c in self.chunks:
            assert c.doc_type == "pdf"

    def test_document_date_at_top_level(self):
        for c in self.chunks:
            assert c.document_date == "2026-01-01"

    def test_content_hash_at_top_level(self):
        for c in self.chunks:
            assert c.content_hash == "abc123"

    def test_no_nested_metadata(self):
        for c in self.chunks:
            d = asdict(c)
            assert "metadata" not in d, "metadata dict should not exist on flat Chunk"

    def test_page_number_assigned(self):
        assert any(c.page_number > 0 for c in self.chunks)

    def test_chunk_indices_sequential(self):
        indices = [c.chunk_index for c in self.chunks]
        assert indices == list(range(len(self.chunks)))


# ══════════════════════════════════════════════════════════════════════════════
# Email Chunker
# ══════════════════════════════════════════════════════════════════════════════

class TestEmailChunker:

    # ── Single chunk (short email) ─────────────────────────────────────────

    def test_short_email_single_chunk(self):
        doc    = _make_email("This is a short email body.")
        t0     = time.time()
        chunks = chunk_email_document(doc)
        elapsed = time.time() - t0
        print(f"\n  Short email chunking time: {elapsed:.4f}s")
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "email"

    def test_single_chunk_no_fragment_fields(self):
        chunks = chunk_email_document(_make_email("Please find the attached contract for your review and approval."))
        assert chunks[0].fragment_index     == -1
        assert chunks[0].parent_chunk_index == -1

    # ── Fragment split (long email) ────────────────────────────────────────

    def test_long_email_splits_to_fragments(self):
        para   = _word_block(250)
        body   = f"{para}\n\n{para}\n\n{para}"
        t0     = time.time()
        chunks = chunk_email_document(_make_email(body), token_budget=512)
        elapsed = time.time() - t0
        print(f"\n  Long email chunking time: {elapsed:.4f}s")
        assert len(chunks) > 1
        for c in chunks:
            assert c.chunk_type    == "email_fragment"
            assert c.token_count   <= 512

    def test_total_fragment_count(self):
        para   = _word_block(250)
        body   = f"{para}\n\n{para}\n\n{para}"
        chunks = chunk_email_document(_make_email(body), token_budget=512)
        print(f"\n  Total email fragments: {len(chunks)}")
        assert len(chunks) >= 2

    def test_fragments_parent_chunk_index(self):
        para   = _word_block(250)
        body   = f"{para}\n\n{para}\n\n{para}"
        chunks = chunk_email_document(_make_email(body), chunk_index_start=5, token_budget=512)
        for c in chunks:
            assert c.parent_chunk_index == 5

    def test_fragments_sequential_fragment_index(self):
        para   = _word_block(250)
        body   = f"{para}\n\n{para}\n\n{para}"
        chunks = chunk_email_document(_make_email(body), token_budget=512)
        assert [c.fragment_index for c in chunks] == list(range(1, len(chunks) + 1))

    # ── Metadata propagation ───────────────────────────────────────────────

    def test_metadata_propagated_to_all_chunks(self):
        doc = _make_email(
            "Please find the attached contract for your review and approval.",
            sender="alice@example.com",
            recipients="bob@example.com",
            cc="carol@example.com",
            bcc="dave@example.com",
            date="2001-06-01",
            subject="Contract Review",
        )
        chunks = chunk_email_document(doc)
        c = chunks[0]
        assert c.sender     == "alice@example.com"
        assert c.recipients == "bob@example.com"
        assert c.cc         == "carol@example.com"
        assert c.bcc        == "dave@example.com"
        assert c.date       == "2001-06-01"
        assert c.subject    == "Contract Review"

    # ── Empty body ─────────────────────────────────────────────────────────

    def test_empty_body_returns_no_chunks(self):
        chunks = chunk_email_document(_make_email(""))
        assert chunks == []

    def test_whitespace_only_body_returns_no_chunks(self):
        chunks = chunk_email_document(_make_email("   \n\n  "))
        assert chunks == []


# ══════════════════════════════════════════════════════════════════════════════
# JSON Chunker
# ══════════════════════════════════════════════════════════════════════════════

class TestJSONChunker:

    # ── _split_by_words ────────────────────────────────────────────────────

    def test_split_by_words_respects_budget(self):
        text  = _word_block(600)
        parts = _split_by_words(text, token_budget=200)
        for p in parts:
            assert _tokens(p) <= 200

    def test_split_by_words_no_words_lost(self):
        text  = " ".join([f"w{i}" for i in range(400)])
        parts = _split_by_words(text, token_budget=100)
        assert " ".join(parts) == text

    def test_split_by_words_short_text_unchanged(self):
        text  = "short text"
        parts = _split_by_words(text, token_budget=512)
        assert parts == [text]

    # ── _kv_blocks ─────────────────────────────────────────────────────────

    def test_kv_blocks_one_block_per_key(self):
        record = {"provision": "Some text.", "label": "termination", "source": "file.pdf"}
        blocks = _kv_blocks(record, token_budget=512)
        assert len(blocks) == 3

    def test_kv_blocks_oversized_value_split(self):
        record = {"provision": _word_block(600)}
        blocks = _kv_blocks(record, token_budget=512)
        assert len(blocks) > 1
        for b in blocks:
            assert _tokens(b) <= 512

    def test_kv_blocks_list_value_joined(self):
        record = {"tags": ["alpha", "beta", "gamma"]}
        blocks = _kv_blocks(record, token_budget=512)
        assert "alpha, beta, gamma" in blocks[0]

    # ── _greedy_merge ──────────────────────────────────────────────────────

    def test_greedy_merge_packs_small_blocks(self):
        blocks = ["block one", "block two", "block three"]
        merged = _greedy_merge(blocks, token_budget=512)
        assert len(merged) == 1
        assert "block one" in merged[0] and "block three" in merged[0]

    def test_greedy_merge_respects_budget(self):
        # Use blocks of ~150 tokens each so merged pairs (~300) stay within budget
        block  = _word_block(100)   # ~133 tokens
        merged = _greedy_merge([block, block, block, block], token_budget=300)
        # Each merged chunk should not exceed budget (two 133-token blocks = 266 ≤ 300)
        for m in merged:
            assert _tokens(m) <= 300

    # ── chunk_ledgar_file ──────────────────────────────────────────────────



    def test_token_budget_never_exceeded(self, tmp_path):
        records = [{"provision": _word_block(600), "label": "x", "source": "y"}]
        f = tmp_path / "test.jsonl"
        f.write_text(json.dumps(records[0]))
        chunks = chunk_ledgar_file(str(f))
        for c in chunks:
            assert c.token_count <= 512, f"Chunk exceeds budget: {c.token_count}"

    def test_oversized_record_has_fragment_index(self, tmp_path):
        records = [{"provision": _word_block(600), "label": "x", "source": "y"}]
        f = tmp_path / "test.jsonl"
        f.write_text(json.dumps(records[0]))
        chunks = chunk_ledgar_file(str(f))
        split  = [c for c in chunks if c.record_fragment_index != -1]
        assert len(split) > 0

    def test_greedy_merge_combines_small_records(self, tmp_path):
        records = [{"provision": "Short clause.", "label": "x", "source": "y"} for _ in range(10)]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records))
        chunks = chunk_ledgar_file(str(f))
        # 10 short records should merge into fewer than 10 chunks
        assert len(chunks) < 10

    def test_empty_provision_skipped(self, tmp_path):
        records = [
            {"provision": "", "label": "x", "source": "y"},
            {"provision": "Valid legal provision text here.", "label": "a", "source": "b"},
        ]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records))
        chunks = chunk_ledgar_file(str(f))
        texts  = " ".join(c.text for c in chunks)
        assert "Valid legal provision" in texts

    def test_max_records_limit(self, tmp_path):
        # Use ~200-token records so they don't all merge into 1 chunk
        records = [{"provision": _word_block(150), "label": "x", "source": "y"} for i in range(20)]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records))
        all_chunks     = chunk_ledgar_file(str(f))
        limited_chunks = chunk_ledgar_file(str(f), max_records=5)
        assert len(limited_chunks) < len(all_chunks)

    def test_chunk_indices_sequential(self, tmp_path):
        records = [{"provision": f"Clause {i}.", "label": "x", "source": "y"} for i in range(10)]
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in records))
        chunks  = chunk_ledgar_file(str(f))
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))
