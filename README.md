# LegalLens
Intelligent legal document search with hybrid retrieval and source citations.

---

## What I Built and Why

LegalLens is a hybrid search system for legal documents. It ingests three document types: PDF contracts, plain-text emails, and JSON compliance provisions. It chunks them with structure-aware logic, embeds them with a local sentence transformer, and indexes them into Elasticsearch. At query time, it runs BM25 full-text search and kNN vector search in parallel, merges results via Reciprocal Rank Fusion (RRF), and returns the top results with full provenance (filename, page, section, date, sender).

The core problem I was solving: legal search fails when it is purely keyword-based because it misses semantically related clauses, or purely semantic because it returns confidently wrong results for off-topic queries. Hybrid search fixes both. A lawyer searching "circumstances preventing contract performance" finds force majeure clauses (semantic).

---

## What I Left Out and Why

**Word documents (.docx):** Trivial to add via `python-docx` but adds no architectural differentiation. The three chosen types demonstrate three fundamentally different ingestion and chunking strategies:

* OCR-based (PDF)
* Metadata-rich plain text (email)
* Schema-inconsistent structured data (JSON)

**ZIP bundle ingestion:** Relevant for M&A due diligence since documents arrive as ZIP archives. Requires a pre-processing layer to extract, classify, and route to the right ingester. Deferred due to time constraint.

**XML ingestion:** A valid compliance format such as RegTrack exports. JSON covers the same compliance story, so supporting XML in parallel adds schema complexity without differentiation. Deferred due to time constraint.

**Access controls enforcement:** The schema is designed for it. The ES Platinum version handles document-level access control.

---

## Architectural Decisions

**Elasticsearch over ChromaDB or Qdrant:** ChromaDB is purpose-built for vector search but has no native full-text (BM25) capability. Legal documents require exact keyword matching such as clause numbers, regulation citations, party names, and dates that semantic search alone misses. ES 8 provides both BM25 and kNN in a single index, merged natively. It is also the established standard in legal tech and enterprise search.

**Docling for OCR:** Runs fully locally with no API key, handles both scanned and native PDFs through a single unified pipeline, and outputs clean Markdown including tables as Markdown tables.
Alternatives: `pytesseract` has poor quality on low-resolution scans and no table handling. Mistral OCR is higher quality but incurs per-page API costs.
For production at scale, Mistral OCR's concurrent API would be preferred for throughput.

**all-mpnet-base-v2 for embeddings:** 768-dimensional vectors with higher semantic accuracy than the faster `all-MiniLM-L6-v2` which has 384 dimensions. For legal documents, missing a relevant clause is a real risk, so accuracy matters more than embedding speed. Slower speed is acceptable because embedding only runs once at ingestion time. At search time, ES handles kNN lookup in milliseconds regardless of model choice.

**Manual RRF in Python:** ES 8 native RRF requires a Platinum license. Manual implementation is equivalent. Two separate ES queries (BM25 and kNN) each return `top_k × 10` candidates, then merge in Python:

```
RRF score = 1/(60 + rank_bm25 + 1) + 1/(60 + rank_knn + 1)
```

Documents appearing in only one list still get a partial score. This is a union, not an intersection. Results below `MIN_RRF_SCORE = 0.020` are dropped. A document ranked #1 in only one list scores `1/61 ≈ 0.016`, which is below the threshold. This acts as the relevance gate that prevents off-topic queries from returning results.

**Per-type chunking over a universal chunker:** A recursive character chunker with fixed size and overlap ignores document structure. It can split mid-table in a contract or mid-clause in a compliance provision. Each type has a natural semantic boundary:

* `##` headings for PDFs
* Individual emails for email threads
* Individual records for LEDGAR

Tables in PDFs are always treated as atomic.

**Single flat ES index:** All three document types live in one index called `legallens` with a shared flat mapping. Alternatives such as one index per type require cross-index queries at search time and complicate RRF merging. The flat mapping covers all fields. Type-specific fields such as `sender` for emails or `page_number` for PDFs are simply null for other types.

**SHA256 hash caching for PDFs:** On re-ingestion, compute SHA256 of raw PDF bytes and compare against a cached `.hash` sidecar file. If the hash matches, skip OCR entirely and return the cached markdown. Docling is the slowest step, so this is a meaningful performance win. If the hash has changed, the document is reprocessed. Before indexing the new chunks, a delete-by-query removes all old chunks for that filename from ES. Otherwise both old and new chunks would coexist in the index and appear in search results. The `_id` strategy (`{filename}_{chunk_index}`) alone is not sufficient because it overwrites matching indices but leaves orphaned chunks if the new version has fewer chunks than the old.

**ijson for streaming large JSONL files:** LEDGAR's full dataset is over 200MB and expands to roughly 1 to 2GB in memory with `json.load()`. `ijson` parses as a stream with constant memory usage regardless of file size.

---

## Scaling Strategy for 2M Documents

The current prototype handles hundreds of documents. At 2 million documents, every layer needs rethinking:

**Ingestion:**

* Move from sequential file processing to a distributed task queue such as Redis or AWS SQS
* For PDFs, use Mistral OCR since concurrent cloud OCR is 10 to 100 times faster than local Docling for large batches

**Embedding:**

* Switch from CPU inference to GPU batch inference. A single A10G GPU can handle around 500K sentences per hour with all-mpnet-base-v2
* At 2M documents with about 10 chunks each, that is roughly 20M chunks. Use batch size 512 on GPU, parallelized across workers

**Elasticsearch:**

* Move from a single-node setup to a 3-node cluster with 1 replica. This provides high availability and parallel query execution

**Retrieval:**

* Add ES document-level security for access controls so each chunk carries a `matter_id`, and queries are filtered by the authenticated user's clearance

---

## What I Would Do Differently With More Time

**Query understanding layer.** Currently, queries go directly to ES. A lightweight NLP step before search could detect query type, extract named entities such as party names, dates, and clause types, and inject filters into ES accordingly.

**Proper evaluation harness.** The integration tests validate that legal queries return results and off-topic queries do not, but they do not measure precision or recall. With more time, I would build an evaluation set of 50 queries with ground-truth relevant documents from CUAD and measure nDCG@5 and MRR.

---

## How to Run

### Prerequisites
- Docker and Docker Compose
- Python 3.10+

---

### Option A: Run with Docker (recommended)

The easiest way to run everything — no Python environment setup needed.

**1. Build and start both ES and the app:**

```bash
docker-compose up --build
```

This starts Elasticsearch and waits for it to be healthy before the app container is ready.

**2. Add your data:**

```
sample_data/
  contracts/     ← put PDF files here
  emails/        ← put plain-text email files here
  compliance/    ← put .jsonl files here (LEDGAR format)
```

**3. Ingest:**

```bash
docker-compose run app python ingest.py --all

# Or individually
docker-compose run app python ingest.py --pdfs
docker-compose run app python ingest.py --emails
docker-compose run app python ingest.py --compliance --max-records 2000

# Wipe and re-index from scratch
docker-compose run app python ingest.py --all --recreate
```

**4. Search:**

```bash
docker-compose run app python search_cli.py
```

**5. Run tests:**

```bash
docker-compose run app python -m pytest tests/ -v -m "not integration"
```

---

### Option B: Run locally (without Docker)

**1. Start Elasticsearch only:**

```bash
docker-compose up -d elasticsearch
```

Wait ~20 seconds. Check: `curl http://localhost:9200`

**2. Set up Python environment:**

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**3. Add your data:**

```
sample_data/
  contracts/     ← put PDF files here
  emails/        ← put plain-text email files here
  compliance/    ← put .jsonl files here (LEDGAR format)
```

**4. Ingest:**

```bash
python ingest.py --all

# Or individually
python ingest.py --pdfs
python ingest.py --emails
python ingest.py --compliance --max-records 2000

# Wipe and re-index from scratch
python ingest.py --all --recreate
```

Ingestion saves intermediate files:
- `sample_data/contracts/<name>.md` — Docling markdown for each PDF
- `sample_data/contracts/chunks_output.json` — PDF chunks
- `sample_data/emails/chunks_output.json` — email chunks
- `sample_data/compliance/chunks_output.json` — compliance chunks

**5. Search:**

```bash
python search_cli.py
```

Type any legal question. Enter `quit` to exit.

Example queries that work well:
- `force majeure clause`
- `payment obligations thirty days invoice`
- `termination upon written notice`
- `governing law New York arbitration`

Off-topic queries (e.g. `what is the weather today`) return zero results — by design.

**6. Run tests:**

```bash
# Unit tests only — no ES required
python -m pytest tests/ -v -m "not integration"

# All tests including integration — requires ES running with indexed data
python -m pytest tests/ -v
```

---

## Project Structure

```
LegalLens/
├── ingestion/          # Document loaders (PDF via Docling, email line parser, ijson streamer)
├── chunking/           # Per-type chunkers (RSM for PDF, fragment split for email, KV merge for JSON)
├── embedding/          # Batch embedder (all-mpnet-base-v2, batch size 64, normalize=True)
├── storage/            # ES client, index mapping, bulk indexer
├── retrieval/          # Hybrid search: BM25 + kNN → RRF merge → relevance gate
├── tests/              # Unit tests (mocked ES) + integration tests (real ES + data)
├── sample_data/        # contracts/, emails/, compliance/
├── ingest.py           # Full ingestion pipeline CLI
├── search_cli.py       # Interactive search interface
├── Dockerfile          # App container (Python 3.11 + dependencies)
├── docker-compose.yml  # ES + app services with healthcheck
└── DECISIONS.md        # Full reasoning behind every architectural choice
```
