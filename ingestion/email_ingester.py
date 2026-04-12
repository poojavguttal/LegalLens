import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("email_ingester")

_HEADER_FIELDS = {"message-id", "date", "from", "to", "cc", "bcc", "subject", "file-name"}

_FORWARDED_BY_RE = re.compile(
    r'^\s*-{3,}\s*Forwarded by (.+?) on (\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}).+?-{3,}',
    re.IGNORECASE
)
_ANY_QUOTE_RE = re.compile(
    r'(\s*-{3,}\s*(?:Original Message|Forwarded by).+)',
    re.IGNORECASE
)
_FROM_RE    = re.compile(r'^From\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
_SENT_RE    = re.compile(r'^Sent\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)


@dataclass
class ReplyMessage:
    sender: str
    date: str
    text: str
    replied_to: str = ""    # sender of the previous message in the chain


@dataclass
class EmailDocument:
    filename: str
    file_path: str
    doc_type: str = "email"
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    cc: str = ""
    bcc: str = ""
    date: str = ""
    message_id: str = ""
    thread_id: str = ""
    thread_length: int = 1
    body: str = ""
    reply_chain: list = field(default_factory=list)   # ReplyMessage list, oldest first
    metadata: dict = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_recipients(raw: str) -> str:
    raw = re.sub(r"[\[\]']", "", raw)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(parts)


def _derive_thread_id(file_name: str, message_id: str) -> str:
    if file_name:
        parts = file_name.strip().rstrip(".").split("/")
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return parts[0]
    return message_id


def _clean_text(text: str) -> str:
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _clean_sender(raw: str) -> str:
    """
    Extract a clean sender string from a raw From: value.
    Priority: email in mailto: → email in <> → bare email → raw string.
    e.g. 'Sara.Shackleton@enron.com [mailto:Sara.Shackleton@enron.com]' → 'Sara.Shackleton@enron.com'
    e.g. 'Bass, Eric <eric.bass@enron.com>' → 'eric.bass@enron.com'
    e.g. 'Bass, Eric' → 'Bass, Eric'
    """
    raw = raw.strip()
    m = re.search(r'mailto:([^\]>\s]+)', raw)
    if m:
        return m.group(1)
    m = re.search(r'<([^>]+@[^>]+)>', raw)
    if m:
        return m.group(1)
    m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', raw)
    if m:
        return m.group(0)
    return raw


def _extract_body_from_original_section(section: str) -> str:
    """
    Strip From/Sent/To/Cc/Subject header lines from an -----Original Message----- section.
    Body starts after the first blank line following the headers.
    Handles multi-line To: fields (continuation lines that don't start a new header).
    """
    lines = section.splitlines()
    body_lines = []
    in_headers = True

    # Skip leading blank lines so we don't prematurely exit header mode
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    lines = lines[start:]

    for line in lines:
        stripped = line.strip()
        if in_headers:
            # Explicit header key: value lines
            if re.match(r'^(From|Sent|To|Cc|Bcc|Subject|Date)\s*:', stripped, re.IGNORECASE):
                continue
            # Continuation lines of a multi-line header (e.g. second line of To:)
            # These are non-blank lines that don't start a new header key
            if stripped and not re.match(r'^\w[\w\s]*:', stripped):
                # Could be continuation — skip if we haven't yet seen a blank line
                continue
            if not stripped:
                in_headers = False
                continue
        body_lines.append(line)

    return _clean_text('\n'.join(body_lines))


def _extract_body_from_forwarded_section(section: str) -> str:
    """
    Strip indented mini-headers from a ----- Forwarded by ----- section.
    Format:
        [blank]
        \tSender Name
        \t12/22/2000 09:39 AM
        \t\t
        \t\t To: ...
        \t\t Subject: ...
        [blank]
        Actual body text starts here (non-indented)
    Body starts at the first non-blank, non-indented line.
    """
    lines = section.splitlines()
    body_lines = []
    found_body = False

    for line in lines:
        stripped = line.strip()
        if found_body:
            body_lines.append(line)
            continue
        # Skip blank lines before body
        if not stripped:
            continue
        # Skip indented lines (mini-headers: name, timestamp, To, cc, Subject)
        if line.startswith('\t') or line.startswith('   '):
            continue
        # First non-blank, non-indented line = body starts
        found_body = True
        body_lines.append(line)

    return _clean_text('\n'.join(body_lines))


# ── Parse quoted sections into reply chain ─────────────────────────────────────

def _build_reply_chain(top_sender: str, top_date: str, top_text: str, body: str) -> list[ReplyMessage]:
    """
    Split the body at quote markers and build a reply chain.
    Returns messages in chronological order (oldest → newest).
    """
    raw_sections = _ANY_QUOTE_RE.split(body)

    messages = []

    # First part = top-level email text (newest)
    messages.append(ReplyMessage(
        sender=top_sender,
        date=top_date,
        text=_clean_text(top_text),
    ))

    i = 1
    while i < len(raw_sections) - 1:
        marker  = raw_sections[i].strip()
        section = raw_sections[i + 1] if i + 1 < len(raw_sections) else ""

        is_forwarded = bool(re.search(r'Forwarded by', marker, re.IGNORECASE))

        if is_forwarded:
            m = _FORWARDED_BY_RE.match(marker)
            sender = _clean_sender(m.group(1)) if m else ""
            date   = m.group(2) if m else ""
            text   = _extract_body_from_forwarded_section(section)
        else:
            # -----Original Message-----
            m_from = _FROM_RE.search(section)
            m_sent = _SENT_RE.search(section)
            sender = _clean_sender(m_from.group(1)) if m_from else ""
            date   = m_sent.group(1).strip() if m_sent else ""
            text   = _extract_body_from_original_section(section)

        if text:
            messages.append(ReplyMessage(sender=sender, date=date, text=text))

        i += 2

    # Reverse to chronological order (oldest first)
    messages = list(reversed(messages))

    # Wire up replied_to: each message was replied to by the next
    for idx in range(len(messages) - 1):
        messages[idx + 1].replied_to = messages[idx].sender

    return messages


# ── Main ingester ──────────────────────────────────────────────────────────────

def ingest_email_file(file_path: str) -> EmailDocument:
    """
    Parse a plain-text email .txt file.
    Extracts top-level headers and builds a reply_chain (oldest → newest)
    showing who replied to whom with clean text for each message.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    raw   = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    headers    = {}
    body_lines = []
    in_body    = False
    last_key   = None

    for line in lines:
        if in_body:
            body_lines.append(line)
            continue
        if not line.strip():
            in_body = True
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            if key.strip().lower() in _HEADER_FIELDS:
                last_key = key.strip().lower()
                headers[last_key] = value.strip()
                continue
        # Continuation line of a multi-line header (e.g. Cc: spanning two lines)
        if last_key and (line.startswith(" ") or line.startswith("\t")):
            headers[last_key] = headers[last_key] + " " + line.strip()
            continue
        in_body = True
        body_lines.append(line)

    full_body  = _clean_text("\n".join(body_lines))
    message_id = headers.get("message-id", "")
    file_name  = headers.get("file-name", "")
    sender     = headers.get("from", "")
    date       = headers.get("date", "")[:10]

    # Top-level text = everything before first quote marker
    top_parts = _ANY_QUOTE_RE.split(full_body)
    top_text  = top_parts[0].strip() if top_parts else full_body

    reply_chain   = _build_reply_chain(sender, date, top_text, full_body)
    thread_length = len(reply_chain) - 1   # 0 = standalone, 1+ = number of replies

    metadata = {
        "filename":      path.name,
        "file_path":     str(path.resolve()),
        "doc_type":      "email",
        "subject":       headers.get("subject", ""),
        "sender":        sender,
        "recipients":    _parse_recipients(headers.get("to", "")),
        "cc":            _parse_recipients(headers.get("cc", "")),
        "date":          date,
        "message_id":    message_id,
        "thread_id":     _derive_thread_id(file_name, message_id),
        "thread_length": thread_length,
    }

    logger.info(f"{path.name} | from={sender} | date={date} | thread_length={thread_length}")

    return EmailDocument(
        filename=path.name,
        file_path=str(path.resolve()),
        subject=headers.get("subject", ""),
        sender=sender,
        recipients=_parse_recipients(headers.get("to", "")),
        cc=_parse_recipients(headers.get("cc", "")),
        bcc=_parse_recipients(headers.get("bcc", "")),
        date=date,
        message_id=message_id,
        thread_id=_derive_thread_id(file_name, message_id),
        thread_length=thread_length,
        body=full_body,
        reply_chain=reply_chain,
        metadata=metadata,
    )


def ingest_emails_directory(directory: str) -> list[EmailDocument]:
    """Ingest all .txt email files in a directory."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    txt_files = sorted(dir_path.glob("*.txt"))
    if not txt_files:
        logger.warning(f"No .txt files found in: {directory}")
        return []

    results = []
    for fp in txt_files:
        try:
            results.append(ingest_email_file(str(fp)))
        except Exception as e:
            logger.error(f"Failed to ingest {fp.name}: {e}")

    logger.info(f"Ingested {len(results)} emails from {directory}")
    return results
