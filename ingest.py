"""
Full ingestion pipeline: ingest → chunk → save chunks JSON → embed → index in ES

Usage:
    python ingest.py --all                          # run everything
    python ingest.py --pdfs                         # only PDFs
    python ingest.py --emails                       # only emails
    python ingest.py --compliance                   # only compliance JSON
    python ingest.py --all --max-records 500        # limit LEDGAR records
    python ingest.py --all --recreate               # wipe ES index first
"""
import argparse
import dataclasses
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CONTRACTS_DIR  = Path("sample_data/contracts")
EMAILS_DIR     = Path("sample_data/emails")
COMPLIANCE_DIR = Path("sample_data/compliance")


# ── PDF pipeline ───────────────────────────────────────────────────────────────

def run_pdfs(es, recreate=False):
    from ingestion.pdf_ingester import ingest_pdf
    from chunking.pdf_chunker import chunk_pdf
    from embedding.embedder import embed_chunks
    from storage.indexer import index_docs

    all_chunks = []
    for pdf_path in sorted(CONTRACTS_DIR.rglob("*.pdf")):
        print(f"\nIngesting {pdf_path.name}...")
        doc    = ingest_pdf(str(pdf_path))
        chunks = chunk_pdf(
            doc.markdown,
            filename=doc.filename,
            doc_type=doc.doc_type,
            document_date=doc.document_date,
            content_hash=doc.content_hash,
        )
        all_chunks.extend(chunks)
        print(f"  {len(chunks)} chunks")

    # Save chunks JSON in contracts/
    out = CONTRACTS_DIR / "chunks_output.json"
    out.write_text(json.dumps([dataclasses.asdict(c) for c in all_chunks], indent=2))
    print(f"\nPDF chunks saved → {out}  ({len(all_chunks)} total)")

    # Embed + index
    docs = embed_chunks(all_chunks)
    n    = index_docs(docs, es=es)
    print(f"Indexed {n} PDF chunks into ES")


# ── Email pipeline ─────────────────────────────────────────────────────────────

def run_emails(es):
    from chunking.email_chunker import chunk_emails_directory
    from embedding.embedder import embed_chunks
    from storage.indexer import index_docs

    chunks = chunk_emails_directory(str(EMAILS_DIR))

    out = EMAILS_DIR / "chunks_output.json"
    out.write_text(json.dumps([dataclasses.asdict(c) for c in chunks], indent=2))
    print(f"Email chunks saved → {out}  ({len(chunks)} total)")

    docs = embed_chunks(chunks)
    n    = index_docs(docs, es=es)
    print(f"Indexed {n} email chunks into ES")


# ── Compliance JSON pipeline ───────────────────────────────────────────────────

def run_compliance(es, max_records=None):
    from chunking.json_chunker import chunk_ledgar_file
    from embedding.embedder import embed_chunks
    from storage.indexer import index_docs

    jsonl_files = list(COMPLIANCE_DIR.glob("*.jsonl"))
    if not jsonl_files:
        print("No .jsonl files found in compliance/")
        return

    all_chunks = []
    for f in jsonl_files:
        print(f"\nChunking {f.name}...")
        chunks = chunk_ledgar_file(str(f), max_records=max_records)
        all_chunks.extend(chunks)
        print(f"  {len(chunks)} chunks")

    out = COMPLIANCE_DIR / "chunks_output.json"
    out.write_text(json.dumps([dataclasses.asdict(c) for c in all_chunks], indent=2))
    print(f"Compliance chunks saved → {out}  ({len(all_chunks)} total)")

    docs = embed_chunks(all_chunks)
    n    = index_docs(docs, es=es)
    print(f"Indexed {n} compliance chunks into ES")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",         action="store_true", help="Run all pipelines")
    parser.add_argument("--pdfs",        action="store_true", help="Run PDF pipeline only")
    parser.add_argument("--emails",      action="store_true", help="Run email pipeline only")
    parser.add_argument("--compliance",  action="store_true", help="Run compliance pipeline only")
    parser.add_argument("--max-records", type=int, default=None, help="Limit LEDGAR records (default: all)")
    parser.add_argument("--recreate",    action="store_true", help="Drop and recreate ES index first")
    args = parser.parse_args()

    from storage.es_client import get_client
    from storage.indexer import create_index
    es = get_client()
    create_index(es, recreate=args.recreate)

    if args.all or args.pdfs:
        run_pdfs(es)
    if args.all or args.emails:
        run_emails(es)
    if args.all or args.compliance:
        run_compliance(es, max_records=args.max_records)


if __name__ == "__main__":
    main()
