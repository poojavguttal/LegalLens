"""
Render a source PDF as a scrollable, self-contained HTML document, highlighting
the matched chunk text on its page.

Matching is done at the word level with punctuation/case stripped, since the
markdown text stored in Elasticsearch (Docling's OCR/export output) frequently
differs from the PDF's own text layer in cosmetic ways (smart quotes, spacing).
Scanned PDFs with no embedded text layer at all can't be highlighted — the page
still renders, just without a highlight.
"""
import base64
import re
from pathlib import Path

import fitz  # PyMuPDF
import streamlit as st

CONTRACTS_DIR = Path("sample_data/contracts")

_MIN_MATCH_WORDS = 6
_MAX_MATCH_WORDS = 14
_TARGET_ANCHOR_ID = "legallens-pdf-target"


def find_pdf_path(filename: str) -> Path | None:
    """Locate the original PDF under sample_data/contracts by filename."""
    if not filename:
        return None
    for path in CONTRACTS_DIR.rglob("*.pdf"):
        if path.name == filename:
            return path
    return None


def _normalize_word(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _page_word_tokens(page: "fitz.Page") -> tuple[list[str], list[fitz.Rect]]:
    tokens, boxes = [], []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        norm = _normalize_word(text)
        if norm:
            tokens.append(norm)
            boxes.append(fitz.Rect(x0, y0, x1, y1))
    return tokens, boxes


def _candidate_tokens(text: str) -> list[str]:
    tokens = [_normalize_word(w) for w in text.split()]
    tokens = [t for t in tokens if t]
    # Drop stray leading page-number artifacts from chunk merges.
    while tokens and tokens[0].isdigit() and len(tokens[0]) <= 3:
        tokens = tokens[1:]
    return tokens


def _find_match(cand_tokens: list[str], page_tokens: list[str]) -> tuple[int, int] | None:
    """Find the longest prefix of cand_tokens that appears in page_tokens. Returns (start_idx, length)."""
    max_win = min(_MAX_MATCH_WORDS, len(cand_tokens))
    for win in range(max_win, _MIN_MATCH_WORDS - 1, -1):
        window = cand_tokens[:win]
        n = len(window)
        for i in range(len(page_tokens) - n + 1):
            if page_tokens[i:i + n] == window:
                return i, n
    return None


@st.cache_data(show_spinner=False)
def render_full_document(
    pdf_path: str,
    target_page: int,
    highlights: list[tuple[int, str]],
    zoom: float = 1.6,
) -> tuple[list[tuple[int, bytes]], set[int], int]:
    """
    Render every page of a PDF to PNG, highlighting each matched excerpt.

    `highlights` is a list of (page_number, excerpt_text) — one entry per chunk
    that matched, so a document cited once for several excerpts still shows all
    of them. The viewer opens at target_page.

    Returns (pages, highlighted_pages, target_page_clamped) where pages is a list
    of (page_number, png_bytes) covering the whole document in order, and
    highlighted_pages holds the page numbers where the excerpt was actually found.
    """
    by_page: dict[int, list[str]] = {}
    for page_number, text in highlights or []:
        if text:
            by_page.setdefault(int(page_number), []).append(text)

    doc = fitz.open(pdf_path)
    try:
        total_pages = doc.page_count
        target_index = max(0, min(target_page - 1, total_pages - 1))
        highlighted_pages: set[int] = set()
        pages = []

        for index in range(total_pages):
            page = doc[index]
            excerpts = by_page.get(index + 1, [])
            if excerpts:
                page_tokens, page_boxes = _page_word_tokens(page)
                for excerpt in excerpts:
                    match = _find_match(_candidate_tokens(excerpt), page_tokens)
                    if match:
                        start, length = match
                        for box in page_boxes[start:start + length]:
                            page.add_highlight_annot(box)
                        highlighted_pages.add(index + 1)

            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            pages.append((index + 1, pix.tobytes("png")))

        return pages, highlighted_pages, target_index + 1
    finally:
        doc.close()


def build_viewer_html(
    pages: list[tuple[int, bytes]],
    target_page: int,
    highlighted_pages: set[int],
    matched_pages: set[int] | None = None,
) -> str:
    """
    Build a self-contained, scrollable HTML view of every page that
    auto-scrolls to target_page when it loads.

    Every page in matched_pages is flagged as a matched section — a document
    cited once may have matched on several pages. Defaults to highlighted_pages.
    """
    matched = set(matched_pages if matched_pages is not None else highlighted_pages)
    matched.add(target_page)

    blocks = []
    for page_num, png_bytes in pages:
        is_target  = page_num == target_page
        is_matched = page_num in matched
        b64 = base64.b64encode(png_bytes).decode("ascii")

        anchor = f'<div id="{_TARGET_ANCHOR_ID}"></div>' if is_target else ""
        border = (
            "border:3px solid #ff4b4b;box-shadow:0 0 12px rgba(255,75,75,0.5);"
            if is_matched else "border:1px solid #ddd;"
        )
        label = ""
        if is_matched:
            note = (
                "highlighted below" if page_num in highlighted_pages
                else "exact text not found — showing page only"
            )
            label = (
                f'<div style="color:#ff4b4b;font:600 13px sans-serif;margin:4px 0;">'
                f"&#9660; Matched section ({note})</div>"
            )

        blocks.append(
            f'<div style="margin:0 auto 18px auto;max-width:900px;">'
            f"{anchor}{label}"
            f'<div style="{border}border-radius:4px;overflow:hidden;">'
            f'<img src="data:image/png;base64,{b64}" style="display:block;width:100%;" />'
            f"</div>"
            f'<div style="text-align:center;color:#888;font:12px sans-serif;margin-top:4px;">Page {page_num}</div>'
            f"</div>"
        )

    return f"""
    <div style="background:#f0f2f6;padding:16px;">
      {''.join(blocks)}
    </div>
    <script>
      function legallensScrollToTarget() {{
        var el = document.getElementById('{_TARGET_ANCHOR_ID}');
        if (el) {{ el.scrollIntoView({{block: 'start', behavior: 'instant'}}); }}
      }}
      legallensScrollToTarget();
      window.addEventListener('load', legallensScrollToTarget);
    </script>
    """
