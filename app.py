"""
LegalLens — Streamlit web interface with NLI query understanding and answer synthesis.

Run:
    streamlit run app.py
"""
import math
import os
import re

import streamlit as st
import streamlit.components.v1 as components

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

st.set_page_config(
    page_title="LegalLens",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("⚖️ LegalLens")
st.caption("Intelligent legal document search — contracts, emails, compliance provisions")

# ── API key guard ──────────────────────────────────────────────────────────────

if not os.getenv("ANTHROPIC_API_KEY"):
    st.error(
        "**ANTHROPIC_API_KEY not set.**  "
        "Add it to a `.env` file in the project root or export it as an environment variable."
    )
    st.stop()

# ── Lazy imports (keep startup fast) ──────────────────────────────────────────

from retrieval.search import search
from retrieval.grouping import group_by_document
from retrieval.confidence import confidence_label
from nli.query_processor import process_query
from nli.synthesizer import synthesize_answer
from pdf_viewer import find_pdf_path, render_full_document, build_viewer_html

# ── PDF viewer modal ───────────────────────────────────────────────────────────


@st.dialog("Document viewer", width="large")
def _pdf_viewer_dialog():
    ctx = st.session_state.get("open_viewer")
    if not ctx:
        return
    highlights = ctx.get("highlights") or []
    with st.spinner("Rendering document…"):
        pages, highlighted_pages, target_page = render_full_document(
            ctx["path"], ctx["page"], highlights
        )
    matched_pages = {page for page, _ in highlights}
    matched_note = f"  ·  {len(matched_pages)} matched page(s)" if len(matched_pages) > 1 else ""
    st.caption(
        f"{ctx['filename']}  ·  {len(pages)} page(s){matched_note}  ·  jumped to page {target_page}"
    )
    components.html(
        build_viewer_html(pages, target_page, highlighted_pages, matched_pages),
        height=650,
        scrolling=True,
    )
    if st.button("Close"):
        st.session_state["open_viewer"] = None
        st.rerun()

# ── Search form ────────────────────────────────────────────────────────────────

with st.form("search_form"):
    query = st.text_input(
        "Ask a legal question",
        placeholder="e.g. What were the grounds for termination in the Arotin case?",
        label_visibility="collapsed",
    )
    col_btn, col_tip = st.columns([1, 5])
    with col_btn:
        submitted = st.form_submit_button("Search", type="primary", use_container_width=True)
    with col_tip:
        st.caption(
            "Try: *force majeure clause* · *payment obligations thirty days* · "
            "*emails about EWEB schedule* · *termination upon written notice*"
        )

# A search only runs on the script-run where the form was actually submitted
# (form_submit_button is True for exactly that one run). Everything below —
# including the "View document" button — reruns the whole script via
# st.rerun(), so the search outcome must be cached in session_state or it
# would vanish (and hit st.stop() below) on every later interaction.
if submitted and query.strip():
    with st.spinner("Understanding your query…"):
        nli = process_query(query)

    with st.spinner("Searching documents…"):
        chunks = search(
            nli["reformulated_query"],
            top_k=5,
            filters=nli.get("filters"),
        )
        # One citation per document, not per chunk. Grouping happens before
        # synthesis so the answer's [n] markers match the source list.
        results = group_by_document(chunks)

    answer = None
    if results:
        with st.spinner("Synthesizing answer…"):
            answer = synthesize_answer(query, results)

    st.session_state["search_state"] = {
        "query": query,
        "nli": nli,
        "results": results,
        "answer": answer,
    }

state = st.session_state.get("search_state")
if not state:
    st.stop()

nli = state["nli"]
results = state["results"]
answer = state["answer"]

with st.expander(f"Query understanding  —  intent: **{nli['intent']}**"):
    st.write(nli["explanation"])
    st.write(f"**Search query sent to ES:** `{nli['reformulated_query']}`")
    active_filters = {k: v for k, v in nli.get("filters", {}).items() if v and v != []}
    if active_filters:
        st.write(f"**Filters applied:** {active_filters}")
    else:
        st.write("**Filters:** none")

if not results:
    st.info(
        "No relevant documents found. "
        "Try rephrasing, or check that documents are indexed (`python ingest.py --all`)."
    )
    st.stop()

st.markdown("### Answer")
st.markdown(answer)

# ── Source results ─────────────────────────────────────────────────────────────

_SOURCE_LINE = re.compile(r"^source:\s*(.+)$", re.MULTILINE)
_LABEL_LINE  = re.compile(r"^label:\s*(.+)$",  re.MULTILINE)


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def _distinct(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in (v.strip() for v in values):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _summarise(values: list[str], keep: int = 2) -> str:
    """Join the first few values, noting how many were elided."""
    head = " · ".join(values[:keep])
    extra = len(values) - keep
    return f"{head} (+{extra} more)" if extra > 0 else head


# Bands match confidence_label(): high / moderate / low
_RING_COLORS = {"high": "#16a34a", "moderate": "#f59e0b", "low": "#ef4444"}


def _confidence_ring(confidence: float, size: int = 52) -> str:
    """A donut gauge of the confidence percentage, as inline SVG."""
    confidence = max(0.0, min(1.0, confidence))
    percent    = round(confidence * 100)
    color      = _RING_COLORS[confidence_label(confidence)]

    stroke        = 5
    radius        = (size - stroke) / 2
    center        = size / 2
    circumference = 2 * math.pi * radius
    filled        = circumference * confidence

    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'role="img" aria-label="{percent}% confidence" style="display:block;margin-left:auto;">'
        # Track — currentColor so it reads correctly in light and dark themes
        f'<circle cx="{center}" cy="{center}" r="{radius:.2f}" fill="none" '
        f'stroke="currentColor" stroke-opacity="0.15" stroke-width="{stroke}"/>'
        f'<circle cx="{center}" cy="{center}" r="{radius:.2f}" fill="none" '
        f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.2f} {circumference - filled:.2f}" '
        f'transform="rotate(-90 {center} {center})"/>'
        f'<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" '
        f'font-family="system-ui,-apple-system,sans-serif" font-size="{size * 0.26:.0f}" '
        f'font-weight="700" fill="{color}">{percent}%</text>'
        f"</svg>"
    )


def _signal_caption(r: dict) -> str:
    """Plain-language breakdown of where a confidence score came from."""
    signals = r.get("_signals") or {}
    parts   = []
    if signals.get("cosine") is not None:
        parts.append(f"semantic similarity {signals['cosine']:.2f}")
    if signals.get("bm25_rank") is not None:
        parts.append(f"keyword rank #{signals['bm25_rank'] + 1}")
    if signals.get("cosine") is not None and signals.get("bm25_rank") is not None:
        parts.append("matched by both retrievers")
    elif signals.get("cosine") is not None:
        parts.append("semantic match only")
    elif signals.get("bm25_rank") is not None:
        parts.append("keyword match only")
    parts.append(f"RRF {r.get('_score')}")
    return "  ·  ".join(parts)


def _compliance_meta(r: dict) -> list[str]:
    """
    Citation metadata for LEDGAR provision chunks.

    Chunks indexed before source_documents/provision_labels existed still carry
    the values inline in the chunk text, so fall back to parsing them out.
    """
    sources = _distinct(_as_list(r.get("source_documents")) or _SOURCE_LINE.findall(r.get("text", "")))
    labels  = _distinct(_as_list(r.get("provision_labels")) or _LABEL_LINE.findall(r.get("text", "")))
    meta = []
    if sources:
        meta.append(_summarise(sources))
    if labels:
        meta.append(_summarise(labels, keep=3))
    return meta


st.divider()
st.markdown(f"### Sources &nbsp; <small>({len(results)} documents)</small>", unsafe_allow_html=True)

for i, r in enumerate(results, 1):
    chunks = r.get("chunks", [r])

    # Build a readable label for the expander header
    meta: list[str] = []
    if r.get("filename"):
        meta.append(r["filename"])
    if r.get("pages"):
        meta.append("p." + ", ".join(str(p) for p in r["pages"]))
    elif r.get("page_number"):
        meta.append(f"p.{r['page_number']}")
    if r.get("section_header") and str(r["section_header"]).strip():
        meta.append(r["section_header"].strip()[:55])
    if r.get("document_date") or r.get("date"):
        meta.append(r.get("document_date") or r.get("date"))
    if r.get("sender"):
        meta.append(f"from: {r['sender']}")
    if r.get("subject"):
        meta.append(f"re: {r['subject'][:40]}")

    chunk_badge = r.get("chunk_type", "")
    if not meta and chunk_badge == "json_provision":
        meta = _compliance_meta(r)

    confidence = r.get("_confidence", 0.0)
    label = (
        f"[{i}]  {' · '.join(meta) if meta else 'Unknown'}  —  "
        f"{round(confidence * 100)}% confidence  `{chunk_badge}`"
    )

    # Expander on the left, confidence ring pinned to the right edge
    col_source, col_ring = st.columns([14, 1], vertical_alignment="center")

    with col_ring:
        st.markdown(_confidence_ring(confidence), unsafe_allow_html=True)

    with col_source, st.expander(label):
        # Only the best-matching excerpt — the rest of the document's matches
        # are highlighted in the viewer rather than repeated here.
        st.markdown(chunks[0]["text"])
        if len(chunks) > 1:
            extra_pages = sorted({p for p in (c.get("page_number") for c in chunks[1:]) if p})
            where = f" on p.{', '.join(str(p) for p in extra_pages)}" if extra_pages else ""
            st.caption(
                f"+{len(chunks) - 1} more matching excerpt(s){where} in this document — "
                "open it to read them in context."
            )

        st.caption(
            f"**{round(confidence * 100)}% confidence** "
            f"({confidence_label(confidence)}) — {_signal_caption(chunks[0])}"
        )

        pdf_path = find_pdf_path(r.get("filename", "")) if r.get("doc_type") == "pdf" else None
        if pdf_path:
            st.divider()
            if st.button("📄 View document", key=f"view_{i}"):
                st.session_state["open_viewer"] = {
                    "path": str(pdf_path),
                    "page": int(chunks[0].get("page_number") or 1),
                    "highlights": [
                        (int(c.get("page_number") or 1), c["text"]) for c in chunks
                    ],
                    "filename": pdf_path.name,
                }
                st.rerun()
        elif r.get("doc_type") == "pdf":
            st.caption(f"Original PDF not found for `{r.get('filename', 'unknown')}`.")

if st.session_state.get("open_viewer"):
    _pdf_viewer_dialog()
