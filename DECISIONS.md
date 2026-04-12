# DECISIONS.md — LegalLens

Architectural and engineering decisions made throughout the build process.
Each entry is timestamped and captures: the decision, alternatives considered, reasoning, and trade-offs.

---

## [2026-04-11] Project Scope — Which Document Types to Support

**Decision:** Support four document types for the prototype: PDFs (contracts), Emails (plain text), and JSON (compliance data) and Spreadsheets. 

**Alternatives considered:**
- Word documents (`.docx`) — deferred. Trivial and adds minimal demonstration value.
- XML (RegTrack compliance exports) — deferred. JSON covers the same compliance story. Supporting both XML and JSON schemas in the prototype adds complexity without differentiation.
- ZIP bundles (M&A due diligence) — explicitly out of scope. Rachel's request is valid but adds a pre-processing layer (extraction + classification inside ZIP) that would consume too much time relative to its prototype value.


**Reasoning:** The three chosen types cover three fundamentally different ingestion and chunking strategies — OCR-based (PDF), metadata-rich plain text (email), and schema-inconsistent structured data (JSON). This demonstrates range and architectural thinking more effectively than covering five similar document types.

---

## [2026-04-11] OCR Engine — Docling over Gemini Vision / Tesseract / Mistral

**Decision:** Use Docling (IBM, open-source) as the OCR engine for both scanned and native PDFs.

**Alternatives considered:**
- `pytesseract` — open-source but poor quality on low-resolution or barely legible scans. Struggles with tables. Requires Tesseract binary installation, increasing Docker complexity.
- `pdfplumber` — excellent for native PDFs with a proper text layer, but zero OCR capability for scanned documents.
- Gemini Vision API (free tier) — high quality, introduces an external API dependency that complicates Docker reproducibility.
- Mistral OCR API — excellent quality, Not used for the prototype because it incurs per-page API costs and requires an external API key, reducing local reproducibility.


**Reasoning:** Docling runs fully locally, requires no API key, produces clean Markdown output (including tables as Markdown tables), handles both scanned and native PDFs through a single unified pipeline, and is Docker-friendly. A single unified pipeline means consistent output format feeding into the chunking layer — no conditional logic based on scan/native detection.

**Trade-offs:**
- Docling requires more RAM than a pure API call approach.
- Slower than cloud OCR APIs for large batches.
- In production at 2M document scale, Mistral OCR's concurrent API approach would be preferred for throughput.

---

## [2026-04-11] Search Engine — Elasticsearch over ChromaDB / Qdrant / Manticore

**Decision:** Use Elasticsearch 8.x as the sole storage and search backend.

**Alternatives considered:**
- **ChromaDB** — simplest setup, purpose-built for vector search, minimal Docker footprint. Rejected because it lacks native full-text (BM25) search — legal documents require exact keyword matching for clause language, citation numbers, and regulation names that semantic search alone misses.
- **ChromaDB + rank_bm25** — combining ChromaDB with a separate BM25 library covers the gap but requires manual score merging, two separate data stores to maintain, and adds complexity without a clean production path.
- **OpenSearch** — essentially free Elasticsearch (Amazon fork), same query DSL. Not chosen because ES 8 basic licence covers all prototype needs and also provides superior performance, advanced ML. Further, when considered about document based access. 

**Reasoning:** Elasticsearch v8+ provides native hybrid search combining BM25 full-text and kNN vector search in a single query, merged automatically via Reciprocal Rank Fusion (RRF). This is critical for legal search where lawyers need both:
- Semantic understanding: "circumstances preventing contract performance" → finds "force majeure" clauses
- Exact keyword matching: specific clause numbers, regulation names, party names, dates

ES also provides built-in result highlighting (shows the exact matched passage in context), native metadata filtering (search only contracts, filter by date range), and is the established standard in legal tech and enterprise search.

**Trade-offs:**
- Higher RAM requirement (~1GB minimum, capped via `ES_JAVA_OPTS`).
- Heavier Docker image (~2GB) vs ChromaDB (~200MB).
- Index mapping must be defined upfront — adds initial setup overhead.
- Security disabled for local prototype (`xpack.security.enabled=false`) — must be re-enabled for production.

---

## [2026-04-11] Hybrid Search Strategy — RRF (Reciprocal Rank Fusion)

**Decision:** Use Elasticsearch's built-in RRF to merge BM25 and kNN scores.

**Alternatives considered:**
- Manual weighted merge: `final_score = 0.5 * semantic_score + 0.5 * bm25_score` — requires tuning weights, which vary by query type. Brittle.
- Re-ranking with a cross-encoder model — high quality but adds latency and model hosting complexity. Deferred to production.

**Reasoning:** RRF is parameter-free, robust across different query types, and is natively supported in ES 8.x. No manual weight tuning required for the prototype.

**Trade-offs:**
- RRF is less tunable than a weighted merge — acceptable for prototype, may need adjustment for production based on user feedback.

---

## [2026-04-11] Embedding Model — sentence-transformers/all-mpnet-base-v2

**Decision:** Use `sentence-transformers/all-mpnet-base-v2` for generating chunk embeddings.

**Alternatives considered:**
- OpenAI `text-embedding-3-small` — higher quality embeddings, but incurs API cost per chunk and introduces external dependency. Rejected for prototype.
- OpenAI `text-embedding-3-large` — even higher quality, higher cost. Rejected.
- `all-MiniLM-L6-v2` — faster (~14K sentences/sec) but lower accuracy (384-dim vectors). Rejected because legal document search prioritises accuracy over embedding speed.
- Legal-domain fine-tuned models (e.g. `legal-bert`) — better for legal terminology but harder to deploy and not available as a simple sentence-transformers model.

**Reasoning:** `all-mpnet-base-v2` produces 768-dimension vectors with higher semantic accuracy than MiniLM. For legal documents, missing a relevant clause or contract term is a real risk — accuracy matters more than embedding speed. The slower embedding speed (~4K sentences/sec) is acceptable because embedding only runs once at ingestion time (one-time cost). At search time, Elasticsearch handles kNN lookup in milliseconds regardless of model choice — so the user-facing speed is not affected.

**Trade-offs:**
- ~3x slower than MiniLM at ingestion time — acceptable since embedding is a one-time cost per document.
- Larger model size (~420MB vs 80MB) — acceptable for prototype.
- Not fine-tuned on legal text — in production, a legal-domain model would improve retrieval quality further.

---

## [2026-04-11] Chunking Strategy — Per Document Type

**Decision:** Implement separate chunking logic for each document type rather than a single universal chunker.

**Alternatives considered:**
- Universal recursive character chunker (512 tokens, 50 overlap) — simple, fast to implement. Rejected because it ignores document structure: splitting mid-table in a contract, or breaking an email thread, destroys semantic coherence.
- Page-based chunking for PDFs — one chunk per page. Rejected because pages are an arbitrary boundary that splits clauses and sections mid-sentence.

**Reasoning:**
- **PDFs:** Section headings are the natural semantic boundary. Tables must be preserved as atomic units (a payment schedule split across chunks becomes meaningless).
- **Emails:** Each email is already a self-contained unit. Thread ID metadata links related emails without requiring them to be concatenated — preserving individual email provenance.
- **JSON:** Each normalized compliance record represents a discrete regulatory position at a point in time. Records should not be merged or split.

**Trade-offs:**
- More code to maintain (three chunkers vs one).
- Each chunker must be tested independently.

---

## [2026-04-11] Large File Handling — ijson for 200MB+ JSON Files

**Decision:** Use `ijson` (streaming JSON parser) for compliance JSON files instead of `json.load()`.

**Alternatives considered:**
- `json.load()` — simple but loads entire file into memory. A 200MB JSON file becomes ~1-2GB in memory. Unacceptable.
- `pandas` chunked reading — works for tabular JSON but compliance exports are nested objects, not flat tables.

**Reasoning:** `ijson` parses JSON as a stream, processing one record at a time with constant memory usage regardless of file size.

**Trade-offs:**
- Slightly more complex code than `json.load()`.
- Slower than in-memory parsing for small files — acceptable trade-off given the file sizes involved.

---

## [2026-04-11] Access Controls — Architectural Note (Not Implemented)

**Decision:** Access controls are not implemented in the prototype but are designed into the data model.

**Approach for production:**
- Each document and chunk carries a `matter_id` and `clearance_level` metadata field in Elasticsearch.
- At query time, a mandatory filter on `matter_id` is injected based on the authenticated user's clearance — lawyers only see documents they are cleared for.
- ES field-level security and document-level security (available in ES Platinum) would enforce this at the index level.
- Authentication layer (OAuth2 / firm SSO) sits in front of the search API.

**Reasoning:** Designing the metadata schema now (even without enforcement) means access controls can be added without re-indexing the entire corpus.

---

## [2026-04-11] Document Update / Staleness Strategy

**Decision:** Use content hashing to detect document updates and avoid stale results.

**Approach:**
- On ingestion, compute `SHA256` hash of raw document content.
- Store hash in ES document metadata.
- On re-ingestion, compare hash — if unchanged, skip. If changed, delete old chunks and re-index.
- This ensures the search index never shows stale results from superseded document versions.

**Reasoning:** The client explicitly flagged stale results as a pain point with the previous system. Content hashing is a simple, reliable solution that works without a document versioning database.

---

## [2026-04-11] Sample Data Sources

**Decision:** Use publicly available legal datasets for sample data.

**Sources chosen:**
- **CUAD (Contract Understanding Atticus Dataset)** — 510 real commercial contracts in PDF format, publicly available. Covers contracts with tables, appendices, and complex clause structures. Already available locally.
- **LEDGAR** — 60K+ legal provisions from SEC filings in JSONL format. Used for the JSON compliance pipeline. Already available locally (`LEDGAR_2016-2019_clean.jsonl`).
- **Synthetic emails** — generated to simulate negotiation threads with realistic metadata (sender, date, subject, thread_id). EDRM dataset used as reference for structure.

**Reasoning:** CUAD and LEDGAR are the standard benchmarks for legal NLP — using them demonstrates awareness of the domain. Synthetic emails allow control over edge cases (threading, date ranges, confidential matter simulation).

---

## [2026-04-11 15:30] PDF Ingestion — Skip Already Processed Files via SHA256 Hash

**Decision:** On re-ingestion, compute SHA256 hash of the raw PDF bytes and compare against a stored `.hash` file. If hash matches, skip OCR and return the cached markdown.

**Alternatives considered:**
- Check if `.md` file exists only — simpler but doesn't detect file updates with the same filename. Explicitly rejected because the client flagged stale results as a pain point.
- Store hashes in a central `processed.json` — adds a shared state file that becomes a bottleneck for concurrent ingestion.

**Reasoning:** Per-file `.hash` sidecar is self-contained, requires no shared state, and correctly handles the case where a document is updated with the same filename — the core stale result problem the client raised.

**Trade-offs:**
- Two extra files per PDF (`.md` and `.hash`) in the source directory.
- Hash comparison adds a negligible overhead (~1ms) per file.

---

## [2026-04-11 15:30] PDF Chunking — RSM (Recursive Split + Greedy Merge) for Text Sections

**Decision:** Use RSM section tree chunking for non-table text in PDFs. Splits at `##` heading boundaries → paragraphs → sentences. Greedily merges adjacent segments up to a 512-token budget.

**Alternatives considered:**
- Recursive character chunker (fixed size, fixed overlap) — ignores document structure. Splits mid-clause or mid-sentence. Rejected.
- Page-based chunking — page boundaries are arbitrary and split clauses. Rejected.
- Structure-aware chunker — also evaluated. RSM was preferred because it handles both heading-structured and unstructured text gracefully via paragraph fallback.

**Reasoning:** RSM was validated in prior chunking experiments. The section tree respects the document hierarchy that Docling's markdown output preserves.

---

## [2026-04-11 15:30] PDF Chunking — Tables as Atomic Chunks

**Decision:** Markdown tables detected in the PDF output are always indexed as a single chunk, regardless of size. Tables are never split.

**Alternatives considered:**
- Convert table rows to KV blocks (key: value format) and apply RSM row tree — good for spreadsheets and compliance data. Rejected for PDFs because legal tables (payment schedules, defendant lists, obligation matrices) are semantically meaningful only as a whole unit.
- Split large tables at row boundaries — risks splitting a payment schedule mid-entry, making the chunk meaningless.

**Reasoning:** A 51-row defendant list split across two chunks is useless for retrieval — a lawyer searching for a defendant name needs the full context of which case and which side they appear on. Keeping tables atomic preserves this.

**Trade-offs:**
- Large tables may exceed the 512-token budget and produce oversized chunks. Accepted — Elasticsearch and the embedding model can handle larger inputs; retrieval accuracy matters more than strict token uniformity.

---

## [2026-04-11 15:30] PDF Chunking — Chunk Provenance Metadata

**Decision:** Every chunk carries: `filename`, `file_path`, `doc_type`, `page_number`, `section_header`, `chunk_type`, `token_count`, `content_hash`, `document_date`.

**Reasoning:** The client explicitly requires "document name, page number, section, date" in every result. `page_number` is derived from `<!-- page N -->` markers in the markdown. `section_header` is the nearest `##` heading above the chunk. `content_hash` links the chunk back to the exact version of the document it was extracted from — critical for stale result detection. `document_date` is extracted via two-step heuristic: filename regex (`YYYY-MM-DD`) first, then first-page content search (common date patterns).

**Trade-offs:**
- Date extraction is heuristic — works for well-named files and documents with a visible date on page 1. Will miss dates buried deep in the document body. Acceptable for prototype.

---

## [2026-04-11] Email Ingestion — Plain Text Enron Format

**Decision:** Custom ingester (`email_ingester.py`) instead of Python's `email` stdlib.

**Reasoning:** The Enron export format is not valid RFC 2822 — `To:` values are Python list reprs (`['a@enron.com' 'b@enron.com']`), `File-Name` is a non-standard header, and quoted history is embedded in the body rather than as MIME parts. The stdlib parser silently fails on these. A line-by-line parser gives full control over these quirks.

**Key decisions inside the ingester:**
- **Multi-line headers** (Cc, Bcc spanning two lines): tracked `last_key` and appended continuation lines (starting with whitespace) to the previous header value.
- **Reply chain parsing**: split body on `-----Original Message-----` and `----- Forwarded by -----` markers via regex. Two separate extractors handle each format's different header layout.

---

## [2026-04-11] Email Chunking — One Email = One Chunk

**Decision:** Each email is stored as a single chunk. Only split at paragraph boundaries if body exceeds 512 tokens.

**Reasoning:** Emails are already short conversational units. Splitting mid-email breaks meaning. The full thread history (quoted text) stays in `text` — it gives the embedding model full context for who said what.

**Fragment fields** (only on split emails):
- `parent_chunk_index` — all fragments point to the first chunk's index for ES reassembly
- `fragment_index` — 1-based position within the split

Single emails carry neither field.
---

## [2026-04-11] Deferred Features (Out of Scope for Prototype)

| Feature | Reason Deferred |
|---|---|
| ZIP bundle ingestion (M&A due diligence) | Adds pre-processing layer with high complexity relative to prototype value |
| Word document ingestion | Trivial to add (`python-docx`) but not architecturally interesti<br/>ng |
| XML ingestion | JSON covers the compliance story; XML adds schema complexity without differentiation |
| Access controls enforcement | Designed into schema; implementation requires auth layer outside prototype scope |
| Document summarisation | Would require LLM integration; outside retrieval prototype scope |
