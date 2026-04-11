import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from docling.document_converter import DocumentConverter

# Date patterns for content-based extraction (checked against first page text)
_DATE_PATTERNS = [
    re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'),                                           # 2025-11-24
    re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b'),                                       # 11/24/2025
    re.compile(r'\b(January|February|March|April|May|June|July|August|September|'
               r'October|November|December)\s+\d{1,2},?\s+\d{4}\b', re.IGNORECASE),   # November 24, 2025
    re.compile(r'\bDate[d]?\s*:\s*(\w+ \d{1,2},?\s*\d{4})\b', re.IGNORECASE),        # Date: November 24, 2025
]

# Filename date patterns
_FILENAME_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("pdf_ingester")

MAX_WORKERS = 3  # concurrent PDFs


@dataclass
class PDFDocument:
    filename: str
    file_path: str
    doc_type: str = "pdf"
    page_count: int = 0
    content_hash: str = ""
    markdown: str = ""
    document_date: str = ""
    skipped: bool = False
    metadata: dict = field(default_factory=dict)


def _extract_document_date(filename: str, markdown: str) -> str:
    """
    Extract document date:
    1. Try filename first (e.g. 2025-11-24 in filename)
    2. Fall back to first-page content search
    Returns date string or empty string if not found.
    """
    # 1. Try filename
    m = _FILENAME_DATE_RE.search(filename)
    if m:
        return m.group(1)

    # 2. Search first page content only (up to first page marker or 3000 chars)
    first_page = markdown.split("<!-- page 2 -->")[0][:3000]
    for pattern in _DATE_PATTERNS:
        m = pattern.search(first_page)
        if m:
            return m.group(0).strip()

    return ""


def _build_markdown_with_page_markers(doc) -> str:
    """
    Export Docling document to markdown with <!-- page N --> markers
    injected at each page boundary so chunkers can track provenance.
    """
    lines = []
    current_page = None

    for item, _ in doc.iterate_items():
        prov = item.prov[0] if hasattr(item, 'prov') and item.prov else None
        page_no = prov.page_no if prov else current_page

        # Inject page marker when page changes
        if page_no is not None and page_no != current_page:
            lines.append(f"\n<!-- page {page_no} -->\n")
            current_page = page_no

        # Export the item as markdown
        if hasattr(item, 'export_to_markdown'):
            try:
                md = item.export_to_markdown(doc=doc)
            except TypeError:
                md = item.export_to_markdown()
        elif hasattr(item, 'text') and item.text:
            md = item.text
        else:
            continue

        if md and md.strip():
            lines.append(md)

    return "\n".join(lines)


def ingest_pdf(file_path: str) -> PDFDocument:
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {path.suffix}")

    # Compute SHA256 hash for staleness detection
    raw_bytes = path.read_bytes()
    content_hash = hashlib.sha256(raw_bytes).hexdigest()

    md_path = path.with_suffix(".md")
    hash_path = path.with_suffix(".hash")

    # Skip if already processed and file hasn't changed
    if md_path.exists() and hash_path.exists():
        stored_hash = hash_path.read_text().strip()
        if stored_hash == content_hash:
            logger.info(f"Skipping (already processed, unchanged): {path.name}")
            cached_markdown = md_path.read_text(encoding="utf-8")
            document_date = _extract_document_date(path.name, cached_markdown)
            return PDFDocument(
                filename=path.name,
                file_path=str(path.resolve()),
                content_hash=content_hash,
                markdown=cached_markdown,
                document_date=document_date,
                skipped=True,
                metadata={
                    "filename": path.name,
                    "file_path": str(path.resolve()),
                    "doc_type": "pdf",
                    "content_hash": content_hash,
                    "markdown_path": str(md_path.resolve()),
                    "document_date": document_date,
                },
            )

    logger.info(f"Processing {path.name}")
    converter = DocumentConverter()
    result = converter.convert(str(path))
    doc = result.document
    page_count = len(doc.pages) if doc.pages else 0

    # Build markdown with page markers injected at page boundaries
    markdown = _build_markdown_with_page_markers(doc)

    # Extract document date
    document_date = _extract_document_date(path.name, markdown)
    if document_date:
        logger.info(f"Document date extracted: {document_date}")
    else:
        logger.warning(f"Could not extract document date for: {path.name}")

    # Save markdown and hash
    md_path.write_text(markdown, encoding="utf-8")
    hash_path.write_text(content_hash, encoding="utf-8")
    logger.info(f"Markdown saved to: {md_path}")

    return PDFDocument(
        filename=path.name,
        file_path=str(path.resolve()),
        page_count=page_count,
        content_hash=content_hash,
        markdown=markdown,
        document_date=document_date,
        skipped=False,
        metadata={
            "filename": path.name,
            "file_path": str(path.resolve()),
            "doc_type": "pdf",
            "page_count": page_count,
            "content_hash": content_hash,
            "markdown_path": str(md_path.resolve()),
            "document_date": document_date,
        },
    )


def ingest_pdfs_concurrent(file_paths: list[str], max_workers: int = MAX_WORKERS) -> list[PDFDocument]:
    """Process multiple PDFs concurrently with up to max_workers parallel threads."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for i, fp in enumerate(file_paths):
            if i > 0:
                time.sleep(0.5)  # stagger submissions slightly
            futures[executor.submit(ingest_pdf, fp)] = fp

        for future in as_completed(futures):
            fp = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Unrecoverable error for {fp}: {e}")

    return results
