import re
from dataclasses import dataclass, field


# ── Token estimate ─────────────────────────────────────────────────────────────

def _tokens(s: str) -> int:
    return int(len(s.split()) / 0.75) if s and s.strip() else 0


# ── Chunk dataclass ────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    chunk_index: int
    chunk_type: str        # "section", "table", "section_fragment"
    text: str
    token_count: int
    section_header: str = ""
    page_number: int = 0
    metadata: dict = field(default_factory=dict)

    def _build_metadata(self, filename: str, doc_type: str,
                        document_date: str, content_hash: str) -> dict:
        return {
            "filename": filename,
            "doc_type": doc_type,
            "document_date": document_date,
            "content_hash": content_hash,
            "page_number": self.page_number,
            "section_header": self.section_header,
            "chunk_type": self.chunk_type,
            "chunk_index": self.chunk_index,
            "token_count": self.token_count,
        }


# ── Text cleaning ──────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Remove form-fill underscores and collapse excessive blank lines."""
    # Remove lines that are only underscores (empty form fields)
    text = re.sub(r'^[_ ]+$', '', text, flags=re.MULTILINE)
    # Collapse 3+ consecutive blank lines into 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Markdown table detection ───────────────────────────────────────────────────

PAGE_MARKER_RE = re.compile(r'^<!--\s*page\s+(\d+)\s*-->$')


def _split_tables(text: str) -> list[tuple[str, bool, int]]:
    """
    Split markdown text into segments of (text, is_table, page_number).
    - Page markers (<!-- page N -->) are parsed to track provenance.
    - Text segments are split at page boundaries so each segment carries
      the correct page number rather than the page where the block started.
    - Tables are detected as consecutive lines containing '|' with a separator row.
    """
    lines = text.split('\n')
    segments = []
    i = 0
    current_page = 1

    while i < len(lines):
        line = lines[i]

        # Parse page marker
        m = PAGE_MARKER_RE.match(line.strip())
        if m:
            current_page = int(m.group(1))
            i += 1
            continue

        # Detect start of a markdown table
        if '|' in line:
            table_lines = []
            page_at_start = current_page
            while i < len(lines) and ('|' in lines[i] or lines[i].strip() == ''):
                mp = PAGE_MARKER_RE.match(lines[i].strip())
                if mp:
                    current_page = int(mp.group(1))
                    i += 1
                    continue
                if '|' in lines[i]:
                    table_lines.append(lines[i])
                elif table_lines:
                    break
                i += 1
            has_separator = any(re.match(r'^\|[\s\-:|]+\|$', l.strip()) for l in table_lines)
            if has_separator and len(table_lines) > 1:
                segments.append(('\n'.join(table_lines), True, page_at_start))
            else:
                segments.append(('\n'.join(table_lines), False, page_at_start))
        else:
            # Collect non-table text — split into sub-blocks at page boundaries
            # so each sub-block carries the correct page number
            current_block = []
            page_at_start = current_page

            while i < len(lines) and '|' not in lines[i]:
                mp = PAGE_MARKER_RE.match(lines[i].strip())
                if mp:
                    # Flush current block before page changes
                    block = '\n'.join(current_block)
                    if block.strip():
                        segments.append((block, False, page_at_start))
                    current_block = []
                    current_page = int(mp.group(1))
                    page_at_start = current_page
                    i += 1
                    continue
                current_block.append(lines[i])
                i += 1

            block = '\n'.join(current_block)
            if block.strip():
                segments.append((block, False, page_at_start))

    return segments


# ── RSM Section Tree ───────────────────────────────────────────────────────────

class RSMNode:
    __slots__ = ("node_type", "text", "context", "children", "_tok")

    def __init__(self, node_type: str, text: str = "", context: str = ""):
        self.node_type = node_type
        self.text      = text
        self.context   = context
        self.children  = []
        self._tok      = None

    @property
    def token_count(self) -> int:
        if self._tok is None:
            self._tok = _tokens(self.text)
        return self._tok

    def add_child(self, child):
        self.children.append(child)
        self._tok = None


def _build_section_tree(text: str) -> RSMNode:
    root = RSMNode("root")
    heading_re = re.compile(r'^(#{1,6} .+)$', re.MULTILINE)

    parts = re.split(r'\n(?=#{1,6} )', text.strip())
    if not any(heading_re.match(p.split("\n")[0]) for p in parts):
        parts = [s.strip() for s in text.split("\n\n") if s.strip()]

    for part in parts:
        if not part.strip():
            continue
        first_line = part.split("\n")[0]
        ctx = first_line if heading_re.match(first_line) else ""
        section = RSMNode("section", text=part, context=ctx)
        root.add_child(section)

        for para_text in [p.strip() for p in part.split("\n\n") if p.strip()]:
            if para_text == ctx:
                continue
            sents = re.split(r'(?<=[.!?])\s+', para_text)
            para = RSMNode("paragraph", text=para_text, context=ctx)
            section.add_child(para)
            if len(sents) > 1:
                for s in sents:
                    if s.strip():
                        para.add_child(RSMNode("sentence", text=s.strip(), context=ctx))

    return root


def _emergency_split(text: str, budget: int) -> list[str]:
    lines = [l for l in text.split("\n") if l.strip()]
    frags, cur, cur_tok = [], [], 0
    for line in lines:
        lt = _tokens(line)
        if cur and cur_tok + lt > budget:
            frags.append("\n".join(cur))
            cur, cur_tok = [line], lt
        else:
            cur.append(line)
            cur_tok += lt
    if cur:
        frags.append("\n".join(cur))

    final = []
    words_per = max(1, int(budget * 0.75))
    for frag in frags:
        if _tokens(frag) > budget:
            words = frag.split()
            for i in range(0, len(words), words_per):
                final.append(" ".join(words[i:i + words_per]))
        else:
            final.append(frag)
    return final


def _rsm_split(node: RSMNode, budget: int) -> list[RSMNode]:
    if node.node_type in ("root", "sheet"):
        result = []
        for child in node.children:
            result.extend(_rsm_split(child, budget))
        return result

    if node.token_count <= budget:
        return [node]

    if node.children:
        result = []
        for child in node.children:
            result.extend(_rsm_split(child, budget))
        return result

    fragments = _emergency_split(node.text, budget)
    return [
        RSMNode(node.node_type + "_fragment", text=frag, context=node.context)
        for frag in fragments
    ]


def _rsm_merge(leaves: list[RSMNode], budget: int) -> list[tuple[str, str]]:
    """Greedily merge adjacent leaves with the same context. Returns (context, text) pairs."""
    if not leaves:
        return []

    groups = []
    cur_ctx = leaves[0].context
    cur_texts, cur_tok = [], 0

    for leaf in leaves:
        tok = _tokens(leaf.text)
        if leaf.context != cur_ctx or (cur_texts and cur_tok + tok > budget):
            if cur_texts:
                groups.append((cur_ctx, "\n\n".join(cur_texts)))
            cur_ctx, cur_texts, cur_tok = leaf.context, [leaf.text], tok
        else:
            cur_texts.append(leaf.text)
            cur_tok += tok

    if cur_texts:
        groups.append((cur_ctx, "\n\n".join(cur_texts)))

    return groups


# ── Main chunker ───────────────────────────────────────────────────────────────

def chunk_pdf(
    markdown: str,
    filename: str = "",
    doc_type: str = "pdf",
    document_date: str = "",
    content_hash: str = "",
    token_budget: int = 512,
) -> list[Chunk]:
    """
    Chunk a PDF's markdown output into searchable chunks.

    - Markdown tables are kept as atomic chunks (never split).
    - Non-table text is chunked using RSM section tree logic.
    - Empty form fields and excessive blank lines are cleaned before indexing.
    - Each chunk builds its own metadata from its own properties + doc-level fields.
    """
    cleaned = _clean(markdown)
    segments = _split_tables(cleaned)

    chunks = []
    chunk_index = 0
    pending_text_parts = []
    pending_page = 1

    def flush_text(parts: list[str], page: int):
        nonlocal chunk_index
        if not parts:
            return
        combined = "\n\n".join(parts)
        root = _build_section_tree(combined)
        leaves = _rsm_split(root, token_budget)
        merged = _rsm_merge(leaves, token_budget)

        for ctx, body in merged:
            body = body.strip()
            if not body or _tokens(body) < 10:
                continue
            chunk = Chunk(
                chunk_index=chunk_index,
                chunk_type="section",
                text=body,
                token_count=_tokens(body),
                section_header=ctx.lstrip('#').strip(),
                page_number=page,
            )
            chunk.metadata = chunk._build_metadata(filename, doc_type, document_date, content_hash)
            chunks.append(chunk)
            chunk_index += 1

    for segment_text, is_table, page_no in segments:
        if is_table:
            flush_text(pending_text_parts, pending_page)
            pending_text_parts = []

            tok = _tokens(segment_text)
            if tok < 5:
                continue
            chunk = Chunk(
                chunk_index=chunk_index,
                chunk_type="table",
                text=segment_text.strip(),
                token_count=tok,
                section_header="",
                page_number=page_no,
            )
            chunk.metadata = chunk._build_metadata(filename, doc_type, document_date, content_hash)
            chunks.append(chunk)
            chunk_index += 1
        else:
            if not pending_text_parts:
                pending_page = page_no
            elif page_no != pending_page:
                flush_text(pending_text_parts, pending_page)
                pending_text_parts = []
                pending_page = page_no
            pending_text_parts.append(segment_text)

    flush_text(pending_text_parts, pending_page)

    # Post-process: merge small section chunks adjacent to tables
    # - Small headerless chunk AFTER a table → footnote, append to previous table
    # - Small section chunk BEFORE a table → intro text, prepend to next table
    THRESHOLD = 40  # tokens

    def _is_small_section(c):
        return c.chunk_type == "section" and c.token_count < THRESHOLD

    # Pass 1: merge small headerless chunks after a table (footnotes)
    pass1 = []
    for chunk in chunks:
        if (
            _is_small_section(chunk)
            and not chunk.section_header
            and pass1
            and pass1[-1].chunk_type == "table"
        ):
            prev = pass1[-1]
            prev.text = prev.text + "\n\n" + chunk.text
            prev.token_count = _tokens(prev.text)
            prev.metadata["token_count"] = prev.token_count
        else:
            pass1.append(chunk)

    # Pass 2: merge small section chunks before a table (intro labels)
    pass2 = []
    for j, chunk in enumerate(pass1):
        next_chunk = pass1[j + 1] if j + 1 < len(pass1) else None
        if (
            _is_small_section(chunk)
            and next_chunk
            and next_chunk.chunk_type == "table"
        ):
            # Prepend this chunk's text to the next table chunk
            next_chunk.text = chunk.text + "\n\n" + next_chunk.text
            next_chunk.token_count = _tokens(next_chunk.text)
            next_chunk.metadata["token_count"] = next_chunk.token_count
        else:
            pass2.append(chunk)

    # Re-index after merge
    for i, chunk in enumerate(pass2):
        chunk.chunk_index = i
        chunk.metadata["chunk_index"] = i

    return pass2
