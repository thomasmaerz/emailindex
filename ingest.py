#!/usr/bin/env python3
"""
Email Ingestion Script

Parses emails from a Maildir, generates embeddings, and stores in SQLite.
Supports resumable ingestion via checkpoint file.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty
from email.header import decode_header
from email.message import Message
import email
from dateutil import parser as date_parser
from bs4 import BeautifulSoup
from markdownify import markdownify as md
import zstandard as zstd
from embedding_device import resolve_embedding_device
from sentence_transformers import SentenceTransformer
from email_reply_parser import EmailReplyParser

from body_text_cleanup import derive_body_main_text, markdown_to_plain_text

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"
ATTACHMENTS_DIR = BASE_DIR / "attachments"
CHECKPOINT_PATH = BASE_DIR / "ingestion" / "resume.json"
LOG_DIR = BASE_DIR / "ingestion" / "logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_BATCH_SIZE = 64
CHECKPOINT_INTERVAL = 500
ZSTD_COMPRESSION_LEVEL = 3
MAX_EMAILS = 999999

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"ingestion_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


CATEGORY_KEYWORDS = {
    "scheduling": ["meeting", "calendar", "invite", "accepted", "declined", "tentative"],
    "security": ["security", "audit", "compliance", "nist", "cmmc", "assessment", "risk"],
    "infrastructure": ["server", "network", "exchange", "vmware", "domain join", "active directory"],
    "project_management": ["project", "pmo", "requirements", "deliverable", "milestone", "sprint", "scrum"],
    "finance": ["invoice", "payment", "budget", "cost", "purchase", "contract", "renewal"],
    "hr": ["benefits", "enrollment", "performance", "training", "onboarding", "pto"],
    "social": ["coffee", "conversation", "lunch", "birthday", "holiday", "celebration"],
    "vendor": ["demo", "trial", "license", "renewal", "contract", "proposal", "quote"],
    "system_notification": ["undeliverable", "bounce", "auto-reply", "out of office", "delivery status"],
}


def classify_email(record: dict) -> list[str]:
    subject = record.get("subject", "").lower()
    body = record.get("body_markdown", "")[:1500].lower()
    text_to_search = f"{subject} {body}"
    
    matched_categories = set()
    
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if " " in keyword:
                if keyword in text_to_search:
                    matched_categories.add(category)
                    break
            else:
                pattern = re.compile(r'\b' + re.escape(keyword) + r'\b')
                if pattern.search(text_to_search):
                    matched_categories.add(category)
                    break
    
    return sorted(list(matched_categories))


def extract_project_tags(record: dict, db_conn: sqlite3.Connection) -> list[str]:
    """Extract project tags by cross-referencing project_registry."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT name, aliases FROM project_registry")
    projects = cursor.fetchall()
    
    if not projects:
        return []
    
    text_to_search = f"{record.get('subject', '')} {record.get('body_markdown', '')[:2000]}".lower()
    matched = set()
    
    for name, aliases_raw in projects:
        # Check project name
        if name.lower() in text_to_search:
            matched.add(name)
        
        # Check aliases — handle both JSON array and comma-string formats
        if aliases_raw:
            aliases = []
            try:
                aliases = json.loads(aliases_raw)
            except (json.JSONDecodeError, TypeError):
                aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
            
            for alias in aliases:
                if alias.lower() in text_to_search:
                    matched.add(name)
                    break
    
    # Extract CR\d+ patterns
    cr_matches = re.findall(r'CR\d+', record.get('subject', ''))
    matched.update(cr_matches)
    
    return sorted(list(matched))


class EncodingHandler:
    FALLBACK_ENCODINGS = [
        'utf-8', 'iso-8859-1', 'windows-1252', 'us-ascii',
        'gb2312', 'shift_jis', 'euc-kr'
    ]

    @classmethod
    def decode_header_value(cls, header_value: str) -> str:
        if not header_value:
            return ""
        
        parts = decode_header(header_value)
        decoded_parts = []
        
        for content, charset in parts:
            if charset is None:
                charset = 'utf-8'
            
            try:
                if isinstance(content, bytes):
                    decoded_parts.append(content.decode(charset))
                else:
                    decoded_parts.append(content)
            except (UnicodeDecodeError, LookupError):
                decoded = None
                for fallback in cls.FALLBACK_ENCODINGS:
                    try:
                        decoded = content.decode(fallback)
                        break
                    except (UnicodeDecodeError, AttributeError):
                        continue
                
                if decoded is None:
                    decoded = content.decode('utf-8', errors='replace')
                decoded_parts.append(decoded)
        
        return ' '.join(decoded_parts)

    @classmethod
    def get_message_body(cls, message: Message) -> tuple[str, str]:
        html_body = ""
        plain_body = ""
        
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                payload = part.get_payload(decode=True)
                
                if payload is None:
                    continue
                
                if not isinstance(payload, bytes):
                    continue
                
                charset = part.get_content_charset() or 'utf-8'
                text = cls._decode_payload(payload, charset)
                
                if content_type == 'text/html':
                    html_body = text
                elif content_type == 'text/plain' and not plain_body:
                    plain_body = text
        else:
            charset = message.get_content_charset() or 'utf-8'
            payload = message.get_payload(decode=True)
            if payload and isinstance(payload, bytes):
                text = cls._decode_payload(payload, charset)
                content_type = message.get_content_type()
                if content_type == 'text/html':
                    html_body = text
                elif content_type == 'text/plain':
                    plain_body = text
        
        return html_body, plain_body

    @classmethod
    def _decode_payload(cls, payload: bytes, charset: str) -> str:
        for enc in [charset] + cls.FALLBACK_ENCODINGS:
            try:
                return payload.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return payload.decode('utf-8', errors='replace')


class HeaderHandler:
    @classmethod
    def safe_get_header(cls, message, header_name: str, default: str = "") -> str:
        try:
            value = message.get(header_name, default)
            if value is None:
                return default
            return EncodingHandler.decode_header_value(value)
        except Exception:
            return default

    @classmethod
    def extract_message_id(cls, message) -> str:
        msg_id = cls.safe_get_header(message, 'Message-ID')
        
        if msg_id and msg_id.strip():
            msg_id = msg_id.strip().strip('<>')
            return msg_id
        
        return f"<generated-{uuid.uuid4()}@emailindex.local>"

    @classmethod
    def extract_date(cls, message) -> str:
        date_str = cls.safe_get_header(message, 'Date')
        
        if not date_str:
            return "1970-01-01T00:00:00Z"
        
        try:
            dt = date_parser.parse(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            return "1970-01-01T00:00:00Z"

    @classmethod
    def extract_addresses(cls, message, header: str) -> tuple[Optional[str], list[str]]:
        header_value = cls.safe_get_header(message, header)
        if not header_value:
            return None, []
        
        from email.utils import parseaddr
        addresses = []
        name = None
        
        parts = header_value.split(',')
        for part in parts:
            parsed = parseaddr(part.strip())
            if parsed[1]:
                addresses.append(parsed[1])
            if not name and parsed[0]:
                name = parsed[0]
        
        # Fallback: derive from_name from email local-part when parseaddr returns empty
        if not name and addresses:
            local_part = addresses[0].split("@")[0]
            name = local_part.replace(".", " ").replace("_", " ").title()
            logger.debug(f"Derived from_name '{name}' from {addresses[0]}")
        
        return name, addresses


class ThreadHandler:
    @classmethod
    def extract_thread_id(cls, message: Message) -> Optional[str]:
        references = message.get('References', '')
        in_reply_to = message.get('In-Reply-To', '')
        
        ref_ids = references.split() if references else []
        reply_ids = re.findall(r'<[^>]+>', in_reply_to) if in_reply_to else []
        reply_ids = [r.strip('<>') for r in reply_ids]
        
        if ref_ids:
            root_id = ref_ids[0].strip('<>')
        elif reply_ids:
            root_id = reply_ids[0]
        else:
            outlook_tid = message.get('X-Outlook-Thread-ID', '')
            if outlook_tid:
                thread_hash = hashlib.sha256(outlook_tid.encode()).hexdigest()[:16]
                return f"thread-{thread_hash}"
            
            # Fallback: derive thread_id from subject_thread_key when no headers exist
            subject_key = cls.generate_subject_thread_key(message.get('Subject', ''))
            if subject_key and subject_key != 'no-subject':
                subject_hash = hashlib.sha256(subject_key.encode()).hexdigest()[:16]
                return f"thread-subj-{subject_hash}"
            return None
        
        thread_hash = hashlib.sha256(root_id.encode()).hexdigest()[:16]
        return f"thread-{thread_hash}"

    @classmethod
    def generate_subject_thread_key(cls, subject: str) -> str:
        if not subject:
            return "no-subject"
        
        normalized = subject
        prefixes = [
            r'^(re:|fwd:|fw:|aw:|sv:|re:|fw)\s*',
            r'^\[.*?\]\s*',
            r'^\(.*?\)\s*',
        ]
        
        for prefix in prefixes:
            normalized = re.sub(prefix, '', normalized, flags=re.IGNORECASE)
        
        normalized = re.sub(r'[^\w\s]', '', normalized)
        normalized = ' '.join(normalized.split())
        normalized = normalized.lower().strip()
        
        if not normalized:
            return "no-subject"
        
        return normalized


class Converter:
    @staticmethod
    def html_to_markdown(html_content: str) -> str:
        if not html_content:
            return ""
        
        soup = BeautifulSoup(html_content, "html.parser")
        
        for element in soup(["script", "style"]):
            element.decompose()
        
        markdown_text = md(str(soup), heading_style="ATX")
        
        markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
        markdown_text = markdown_text.strip()
        
        return markdown_text

    @staticmethod
    def extract_text_from_html(html_content: str) -> str:
        if not html_content:
            return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for element in soup(["script", "style"]):
            element.decompose()
        return soup.get_text(separator=' ', strip=True)

    @staticmethod
    def format_size(bytes_val: int) -> str:
        size = float(bytes_val)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


def compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def get_attachment_path(thread_id: Optional[str], timestamp: str, filename: str) -> Path:
    year = timestamp[:4]
    month_num = int(timestamp[5:7])
    month_abbr = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ][month_num - 1]
    
    safe_filename = re.sub(r'[^\w\s\-.]', '_', filename)
    if len(safe_filename) > 255:
        name, ext = os.path.splitext(safe_filename)
        safe_filename = name[:255 - len(ext)] + ext
    
    thread_dir = thread_id or "no-thread"
    rel_path = Path("attachments") / year / f"{month_num:02d}_{month_abbr}" / thread_dir / safe_filename
    return rel_path


def compress_eml(raw_bytes: bytes) -> bytes:
    compressor = zstd.ZstdCompressor(level=ZSTD_COMPRESSION_LEVEL)
    return compressor.compress(raw_bytes)


# === Quote Salvage Functions ===

_SIGNATURE_STRIP_PATTERNS = [
    # Mobile & App
    r'(?i)Sent from my (iPhone|Android|Galaxy|iPad|handheld).*',
    r'(?i)Get (Outlook|Mail) for (iOS|Android|Mobile).*',
    r'(?i)Sent from Gmail.*',
    # Visual Separators
    r'^--\s*$',
    r'^[_\-=]{10,}$',
    r'^(\*\*\*+|###+)$',
    # Professional Headers
    r'(?i)Original Message',
    r'(?i)From:.*?Sent:.*?To:.*?(?:Subject:.*?)(?=\n|$)',
    r'(?i)IMPORTANT:.*',
    r'(?i)Please consider the environment before printing.*',
    # Hanging reply headers
    r'^On\s.*\swrote:$',
]

_DISCLAIMER_STRIP_PATTERNS = [
    r'(?is)\bCONFIDENTIALITY NOTICE\b.*$',
    r'(?is)\bThis message and any attachments\b.*$',
    r'(?is)\bThis e-?mail and any attachments\b.*$',
    r'(?is)\bThis communication may contain confidential\b.*$',
]

_GENERIC_IMAGE_TOKEN_PATTERNS = [
    r'(?i)^cid:image\d+$',
    r'(?i)^image\d+$',
    r'(?i)^img\d+$',
    r'(?i)^signature$',
    r'(?i)^signature[_-]?logo$',
    r'(?i)^logo$',
    r'(?i)^image$',
]


def _strip_signatures(text: str) -> str:
    """Remove signature noise from text fragment."""
    for pattern in _SIGNATURE_STRIP_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.MULTILINE)
    return text


def _is_meaningful_candidate(text: str) -> bool:
    if not text:
        return False
    cleaned = text
    for pattern in (
        r'(?i)Sent from my (iPhone|Android|Galaxy|iPad|handheld).*',
        r'(?i)Get (Outlook|Mail) for (iOS|Android|Mobile).*',
        r'(?i)Sent from Gmail.*',
        r'(?i)Begin forwarded message:.*',
        r'(?i)Forwarded message:.*',
    ):
        cleaned = re.sub(pattern, '', cleaned, flags=re.MULTILINE)
    
    cleaned = cleaned.strip()
    if len(cleaned) < 15:
        return False
    if not re.search(r'[a-zA-Z0-9]', cleaned):
        return False
    return True


def _strip_reply_headers(text: str) -> str:
    """Drop quoted reply headers when fresh content appears above them."""
    if not text:
        return ""

    header_markers = (
        "\nFrom:",
        "\n-----Original Message-----",
        "\nOn ",
    )
    for marker in header_markers:
        idx = text.find(marker)
        if idx > 0:
            candidate = text[:idx].strip()
            if _is_meaningful_candidate(candidate):
                return candidate
    return text


def _strip_disclaimers(text: str) -> str:
    if not text:
        return ""
    for pattern in _DISCLAIMER_STRIP_PATTERNS:
        text = re.sub(pattern, '', text)
    return text


def _flatten_layout_artifacts(text: str) -> str:
    if not text:
        return ""

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^[|:\s-]+$', line):
            continue
        if re.match(r'^[|\s]+$', line):
            continue
        if '|' in line:
            cells = [c.strip() for c in line.split('|')]
            cleaned_cells = []
            for cell in cells:
                if not cell:
                    continue
                if re.match(r'^[:-]+$', cell):
                    continue
                cell_clean = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cell)
                cell_clean = cell_clean.strip()
                if cell_clean:
                    cleaned_cells.append(cell_clean)
            if cleaned_cells:
                line = " ".join(cleaned_cells)
            else:
                continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r'<img[^>]*>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'mailto:\S+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'cid:\S+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\|\s*\|\s*\|', ' ', text)
    return text


def _cleanup_image_placeholders(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r'cid:image\d+(?:\.[a-z0-9]+)?', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'(?i)signature[_ -]?logo', ' ', text)

    kept_tokens = []
    tokens = text.split()
    for idx, token in enumerate(tokens):
        normalized = re.sub(r'[^a-z0-9:_-]', '', token.lower())
        if normalized == 'logo':
            prev_normalized = re.sub(r'[^a-z0-9:_-]', '', tokens[idx - 1].lower()) if idx > 0 else ''
            if prev_normalized and prev_normalized not in {'signature', 'image', 'image001'}:
                kept_tokens.append(token)
                continue
        if any(re.fullmatch(pattern, normalized) for pattern in _GENERIC_IMAGE_TOKEN_PATTERNS):
            continue
        kept_tokens.append(token)

    return ' '.join(kept_tokens).strip()


def _derive_body_main_text(body_text: str, body_markdown: str) -> str:
    return derive_body_main_text(body_text=body_text, body_markdown=body_markdown)


def _normalize_for_hash(text: str) -> str:
    """Tier 1: Normalize text for hash comparison."""
    text = _strip_signatures(text)
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^a-z0-9]', '', text)
    return text


def _compute_content_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode()).hexdigest()


def _normalize_salvaged_fragment(text: str) -> str:
    normalized = (text or '').strip()
    normalized = re.sub(r'^(From|Sent|To|Cc|Subject):.*$', '', normalized, flags=re.MULTILINE)
    normalized = _strip_signatures(normalized)
    normalized = _strip_disclaimers(normalized)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized)
    return normalized.strip()


def _is_useful_salvaged_fragment(text: str) -> bool:
    normalized = _normalize_salvaged_fragment(text)
    if not normalized:
        return False
    if len(normalized) < 40:
        return False
    if len(normalized.split()) < 7:
        return False
    return True


def _extract_text_from_html(html_text: str) -> str:
    """Extract text content from raw HTML while preserving structure for quote detection."""
    if not html_text or not html_text.strip():
        return ''
    
    soup = BeautifulSoup(html_text, 'html.parser')
    
    for element in soup(['script', 'style']):
        element.decompose()
    
    text = soup.get_text(separator='\n', strip=True)
    
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    cleaned_lines = []
    header_labels = ('From', 'Sent', 'To', 'Cc', 'Subject', 'Bcc', 'Reply-To', 'Date')
    
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.endswith(':') and line.rsplit(':', 1)[0] in header_labels:
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if not next_line.endswith(':') and not any(next_line.startswith(h + ':') for h in header_labels):
                    merged = line + ' ' + next_line
                    cleaned_lines.append(merged)
                    i += 2
                    continue
        cleaned_lines.append(line)
        i += 1
    
    text_with_quote_start = []
    prev_was_header = False
    for idx, line in enumerate(cleaned_lines):
        is_header = line.startswith('From:') or line.startswith('Sent:') or line.startswith('To:') or line.startswith('Subject:')
        if line.startswith('From:') and idx > 0 and not prev_was_header:
            text_with_quote_start.append('')
        text_with_quote_start.append(line)
        prev_was_header = is_header
    
    text = '\n'.join(text_with_quote_start)
    
    text = re.sub(r'(Subject:[^\n]+)\n([^\n])', r'\1\n\n\2', text)
    
    return text


_OUTLOOK_QUOTE_PATTERNS = [
    re.compile(
        r'(?:^|\n\n)(From:[^\n]+\n'
        r'Sent:[^\n]+\n'
        r'To:[^\n]+\n'
        r'(?:Cc:[^\n]+\n)?'
        r'Subject:[^\n]+\n'
        r'\n'
        r'.+?)(?=\n{2}From:[^\n]+\n|\Z)',
        re.MULTILINE | re.DOTALL
    ),
    re.compile(
        r'(?:^|\n\n)(----- ?Original Message ?-----\n'
        r'.+?)(?=\n{2}----- ?Original Message ?-----\n|\Z)',
        re.MULTILINE | re.DOTALL
    ),
    re.compile(
        r'(?:^|\n\n)(On\s[^\n]+?wrote:\n'
        r'.+?)(?=\n{2}On\s[^\n]+?wrote:\n|\Z)',
        re.MULTILINE | re.DOTALL
    ),
]


def _extract_outlook_quotes(text: str) -> list[str]:
    """Extract quoted reply blocks from Outlook-style emails."""
    quotes = []
    for pattern in _OUTLOOK_QUOTE_PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            cleaned = match.strip()
            if len(cleaned) > 100:
                quotes.append(cleaned)
    return quotes


def _is_duplicate_by_hash(conn: sqlite3.Connection, content_hash: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM emails WHERE content_hash = ?", (content_hash,))
    return cursor.fetchone() is not None


_EMBEDDING_MODEL = None


def _get_embedding_model():
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        _EMBEDDING_MODEL = SentenceTransformer(
            "BAAI/bge-small-en-v1.5", device=resolve_embedding_device()
        )
    return _EMBEDDING_MODEL


def _encode_text_to_embedding(text: str) -> bytes:
    import numpy as np
    model = _get_embedding_model()
    embedding = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return embedding.astype(np.float32).tobytes()


def _is_duplicate_by_similarity(conn: sqlite3.Connection, fragment_text: str, thread_id: Optional[str]) -> bool:
    """Tier 2: Check semantic similarity within thread."""
    if not thread_id:
        return False
    
    cursor = conn.cursor()
    cursor.execute("""
        SELECT embedding FROM emails 
        WHERE thread_id = ? AND embedding IS NOT NULL
    """, (thread_id,))
    
    rows = cursor.fetchall()
    if not rows:
        return False
    
    from sentence_transformers import util
    import numpy as np
    
    try:
        fragment_embedding = _encode_text_to_embedding(fragment_text)
        fragment_vec = np.frombuffer(fragment_embedding, dtype=np.float32)
        
        for row in rows:
            existing_vec = np.frombuffer(row['embedding'], dtype=np.float32)
            similarity = util.cos_sim(fragment_vec, existing_vec).item()
            if similarity >= 0.98:
                return True
    except Exception:
        pass
    
    return False


def _make_salvaged_record(content: str, content_hash: str, parent_record: dict) -> dict:
    """Create a salvaged quoted_reply record."""
    body_main_text = _derive_body_main_text(content, content)
    return {
        'id': str(uuid.uuid4()),
        'message_id': f"salvaged-{uuid.uuid4()}@emailindex.local",
        'thread_id': parent_record['thread_id'],
        'parent_id': parent_record['id'],
        'source': 'quoted_reply',
        'content_hash': content_hash,
        'subject_thread_key': parent_record['subject_thread_key'],
        'timestamp': parent_record['timestamp'],
        'from_address': parent_record['from_address'],
        'from_name': parent_record['from_name'],
        'to_addresses': parent_record['to_addresses'],
        'cc_addresses': parent_record['cc_addresses'],
        'subject': f"[Salvaged] {parent_record['subject']}",
        'body_markdown': content,
        'body_plain': content,
        'body_text': content,
        'body_main_text': body_main_text,
        'x_mailer': None,
        'has_attachments': 0,
        'attachments': '[]',
        'folder': parent_record['folder'],
        'raw_eml': None,
        'embedding': None,
    }


# NOTE: A standalone salvage_quotes.py previously existed but was removed (issue #38).
# It had critical bugs (missing message_id, wrong timestamp, diverged hash normalization).
# Quote salvaging is handled here inline during ingestion.
def salvage_quotes(plain_text: Optional[str] = None, html_text: Optional[str] = None, parent_record: Optional[dict] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Extract quoted fragments from plain text or HTML, deduplicate (Tier 1 + Tier 2), return salvaged records."""
    if parent_record is None or conn is None:
        return []
    parent = parent_record
    db_conn = conn

    # Prefer plain text if available
    text_to_process = None
    if plain_text and plain_text.strip():
        text_to_process = plain_text
    elif html_text and html_text.strip():
        text_to_process = _extract_text_from_html(html_text)
    
    if not text_to_process or not text_to_process.strip():
        return []
    
    salvaged = []
    seen_hashes = set()
    
    quoted_fragments = []
    try:
        fragments = EmailReplyParser.read(text_to_process)
        quoted_fragments = [f for f in fragments.fragments if f.quoted]
    except Exception as e:
        logger.warning(f"Failed to parse email for quotes: {e}")
    
    if not quoted_fragments:
        outlook_quotes = _extract_outlook_quotes(text_to_process)
        for content in outlook_quotes:
            if not _is_useful_salvaged_fragment(content):
                continue
            content_hash = _compute_content_hash(content)
            if content_hash in seen_hashes:
                continue
            seen_hashes.add(content_hash)
            if _is_duplicate_by_hash(db_conn, content_hash):
                continue
            if _is_duplicate_by_similarity(db_conn, content, parent.get('thread_id')):
                continue
            salvaged.append(_make_salvaged_record(content, content_hash, parent))
    
    for fragment in quoted_fragments:
        content = fragment.content.strip()
        if len(content) < 100:
            continue
        if not _is_useful_salvaged_fragment(content):
            continue
        
        content_hash = _compute_content_hash(content)
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        
        if _is_duplicate_by_hash(db_conn, content_hash):
            continue
        
        if _is_duplicate_by_similarity(db_conn, content, parent.get('thread_id')):
            continue
        
        salvaged.append(_make_salvaged_record(content, content_hash, parent))
    
    return salvaged


def init_database(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA journal_mode=WAL")
    logger.info("SQLite WAL mode enabled")
    
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        logger.info("sqlite-vec extension loaded")
    except Exception as e:
        logger.warning(f"sqlite-vec extension not available: {e}")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            message_id TEXT UNIQUE NOT NULL,
            thread_id TEXT,
            subject_thread_key TEXT,
            timestamp TEXT NOT NULL,
            from_address TEXT NOT NULL,
            from_name TEXT,
            to_addresses TEXT NOT NULL,
            cc_addresses TEXT,
            subject TEXT NOT NULL,
            body_markdown TEXT NOT NULL,
            body_plain TEXT,
            body_text TEXT,
            body_main_text TEXT,
            x_mailer TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachments TEXT,
            folder TEXT NOT NULL,
            raw_eml BLOB,
            embedding BLOB,
            source TEXT DEFAULT 'original',
            parent_id TEXT,
            content_hash TEXT,
            sender TEXT,
            recipients TEXT,
            category_tags TEXT,
            project_tags TEXT,
            is_outbound INTEGER
        )
    """)

    cursor.execute("PRAGMA table_info(emails)")
    columns = {row[1] for row in cursor.fetchall()}
    if "body_main_text" not in columns:
        cursor.execute("ALTER TABLE emails ADD COLUMN body_main_text TEXT")
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_timestamp ON emails(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_thread_id ON emails(thread_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_subject_thread_key ON emails(subject_thread_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_from_address ON emails(from_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder)")
    
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
            subject,
            body_text,
            content='emails',
            content_rowid='rowid'
        )
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_insert AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, subject, body_text) VALUES (NEW.rowid, NEW.subject, NEW.body_text);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_delete AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_text) VALUES('delete', OLD.rowid, OLD.subject, OLD.body_text);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_update AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_text) VALUES('delete', OLD.rowid, OLD.subject, OLD.body_text);
            INSERT INTO emails_fts(rowid, subject, body_text) VALUES (NEW.rowid, NEW.subject, NEW.body_text);
        END
    """)
    
    # Email category/project tags FTS for tag-based filtering
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS email_category_fts USING fts5(
            category_tags,
            project_tags,
            content='emails'
        )
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
            INSERT INTO email_category_fts(rowid, category_tags, project_tags) 
            VALUES (new.rowid, new.category_tags, new.project_tags);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
            INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags) 
            VALUES('delete', old.rowid, old.category_tags, old.project_tags);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
            INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags) 
            VALUES('delete', old.rowid, old.category_tags, old.project_tags);
            INSERT INTO email_category_fts(rowid, category_tags, project_tags) 
            VALUES (new.rowid, new.category_tags, new.project_tags);
        END
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attachment_hashes (
            sha256 TEXT PRIMARY KEY,
            first_email_id TEXT,
            path TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_registry (
            name TEXT PRIMARY KEY,
            aliases TEXT,
            summary TEXT,
            created_at TEXT
        )
    """)
    
    try:
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS email_vectors USING vec0(
                email_id TEXT,
                embedding FLOAT[384]
            )
        """)
    except Exception as e:
        logger.warning(f"email_vectors table creation failed: {e}")
    
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {db_path}")


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, 'r') as f:
            return json.load(f)
    return {
        "last_processed_path": None,
        "processed_count": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "errors": []
    }


def save_checkpoint(state: dict):
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def is_duplicate_message_id(conn: sqlite3.Connection, message_id: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,))
    return cursor.fetchone() is not None


def process_attachments(message: Message, email_id: str, thread_id: Optional[str], timestamp: str, db_conn: Optional[sqlite3.Connection]) -> list[dict]:
    import mimetypes
    import re as regex_module
    mimetypes.init()
    
    attachments = []
    
    content_ids = set()
    for part in message.walk():
        cid = part.get('Content-ID')
        if cid:
            content_ids.add(cid.strip('<>'))
    
    html_body, _ = EncodingHandler.get_message_body(message)
    cid_refs = set(regex_module.findall(r'cid:([^"\'>\s]+)', html_body or ''))
    
    for part in message.walk():
        content_disposition = part.get('Content-Disposition', '')
        disposition_lower = content_disposition.lower().strip()
        
        if not disposition_lower:
            continue
        
        is_attachment = 'attachment' in disposition_lower
        is_inline = 'inline' in disposition_lower
        
        if not is_attachment and not is_inline:
            continue
        
        if is_inline and not is_attachment:
            filename = part.get_filename()
            if not filename:
                continue
            cid = part.get('Content-ID', '').strip('<>')
            if cid in cid_refs:
                continue
        
        try:
            filename = part.get_filename()
            if not filename:
                filename = part.get_param('filename', '', 'content-disposition') or ''
            if isinstance(filename, tuple):
                filename = filename[0] or ''
            
            if not filename:
                ext = mimetypes.guess_extension(part.get_content_type() or '.bin')
                filename = f"attachment{ext}"
            
            filename = re.sub(r'[^\w\s\-.]', '_', filename)
            if len(filename) > 255:
                name, ext = os.path.splitext(filename)
                filename = name[:255 - len(ext)] + ext
            
            content = part.get_payload(decode=True)
            if content is None or not isinstance(content, bytes):
                continue
            
            sha256_hash = compute_sha256(content)
            mime_type = mimetypes.guess_type(filename)[0] or part.get_content_type() or 'application/octet-stream'
            content_size = len(content)
            
            existing_path = None
            if db_conn:
                cursor = db_conn.cursor()
                cursor.execute("SELECT path FROM attachment_hashes WHERE sha256 = ?", (sha256_hash,))
                row = cursor.fetchone()
                if row and row[0]:
                    existing_path = row[0]
            
            if existing_path:
                rel_path = Path(existing_path)
            else:
                rel_path = get_attachment_path(thread_id, timestamp, filename)
                full_path = BASE_DIR / rel_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                
                if full_path.exists():
                    full_path.unlink()
                
                with open(full_path, 'wb') as f:
                    f.write(content)
                
                if db_conn:
                    cursor = db_conn.cursor()
                    cursor.execute(
                        """INSERT OR IGNORE INTO attachment_hashes (sha256, first_email_id, path, filename, mime_type, size_bytes, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (sha256_hash, email_id, str(rel_path), filename, mime_type, content_size, datetime.now(timezone.utc).isoformat())
                    )
            
            attachments.append({
                'filename': filename,
                'path': str(rel_path),
                'mime_type': mime_type,
                'size_bytes': content_size,
                'sha256': sha256_hash
            })
        
        except Exception as e:
            logger.warning(f"Error processing attachment: {e}")
            continue
    
    return attachments


def parse_email_file(eml_path: Path, folder: str = "INBOX", db_conn: Optional[sqlite3.Connection] = None, source_path: Optional[str] = None) -> list[dict]:
    """Parse email file and extract quoted fragments. Returns list of records (original + salvaged)."""
    try:
        with open(eml_path, 'rb') as f:
            raw_bytes = f.read()
        
        message = email.message_from_bytes(raw_bytes)
        
        message_id = HeaderHandler.extract_message_id(message)
        thread_id = ThreadHandler.extract_thread_id(message)
        subject = HeaderHandler.safe_get_header(message, 'Subject', '')
        subject_thread_key = ThreadHandler.generate_subject_thread_key(subject)
        timestamp = HeaderHandler.extract_date(message)
        from_name, from_addresses = HeaderHandler.extract_addresses(message, 'From')
        to_name, to_addresses = HeaderHandler.extract_addresses(message, 'To')
        cc_name, cc_addresses = HeaderHandler.extract_addresses(message, 'Cc')
        x_mailer = HeaderHandler.safe_get_header(message, 'X-Mailer') or HeaderHandler.safe_get_header(message, 'User-Agent')
        
        from_address = from_addresses[0] if from_addresses else "unknown@local"
        
        html_body, plain_body = EncodingHandler.get_message_body(message)
        
        email_id = str(uuid.uuid4())
        
        # Build parent record first so we can pass to salvage_quotes
        attachments = process_attachments(message, email_id, thread_id, timestamp, db_conn)
        has_attachments = len(attachments) > 0
        
        parent_record = {
            'id': email_id,
            'message_id': message_id,
            'thread_id': thread_id,
            'subject_thread_key': subject_thread_key,
            'timestamp': timestamp,
            'from_address': from_address,
            'from_name': from_name,
            'to_addresses': json.dumps(to_addresses),
            'cc_addresses': json.dumps(cc_addresses) if cc_addresses else None,
            'subject': subject,
            'body_markdown': '',
            'body_plain': plain_body,
            'body_text': '',
            'x_mailer': x_mailer,
            'has_attachments': 1 if has_attachments else 0,
            'attachments': json.dumps(attachments),
            'folder': folder,
            'raw_eml': compress_eml(raw_bytes),
            'embedding': None,
            'source_path': source_path or str(eml_path),
            'source': 'original',
            'parent_id': None,
            'content_hash': None,
        }
        
        # Extract quoted fragments BEFORE markdown conversion
        salvaged_records = []
        if db_conn and (plain_body or html_body):
            salvaged_records = salvage_quotes(
                plain_text=plain_body,
                html_text=html_body,
                parent_record=parent_record,
                conn=db_conn
            )
        
        # Convert HTML to markdown (original body stays intact with quotes)
        if html_body:
            body_markdown = Converter.html_to_markdown(html_body)
        else:
            body_markdown = plain_body or ""
        
        parent_record['body_markdown'] = body_markdown.strip()
        if html_body:
            html_text = Converter.extract_text_from_html(html_body).strip()
            if html_text:
                parent_record['body_text'] = html_text
            elif plain_body:
                parent_record['body_text'] = plain_body.strip()
            else:
                parent_record['body_text'] = body_markdown.strip()
        elif plain_body:
            parent_record['body_text'] = plain_body.strip()
        else:
            parent_record['body_text'] = body_markdown.strip()
        parent_record['body_main_text'] = _derive_body_main_text(
            body_text=parent_record.get('body_text', ''),
            body_markdown=parent_record.get('body_markdown', ''),
        )
        
        tags = classify_email(parent_record)
        parent_record['category_tags'] = json.dumps(tags)
        parent_record['sender'] = from_address
        all_recipients = to_addresses + (cc_addresses or [])
        parent_record['recipients'] = json.dumps(all_recipients)
        
        # Extract project tags from project_registry
        project_tags = extract_project_tags(parent_record, db_conn) if db_conn else []
        parent_record['project_tags'] = json.dumps(project_tags)
        
        for salvaged in salvaged_records:
            salvaged['category_tags'] = json.dumps(tags)
            salvaged['sender'] = from_address
            salvaged['recipients'] = json.dumps(all_recipients)
            salvaged['project_tags'] = json.dumps(project_tags)
        
        return [parent_record] + salvaged_records
    
    except Exception as e:
        logger.error(f"Error parsing {eml_path}: {e}")
        return []


def generate_embedding_text(record: dict) -> str:
    date_part = record['timestamp'][:10] if record['timestamp'] else ""
    body_source = record.get('body_main_text') or record.get('body_text') or record.get('body_markdown') or ""
    body = body_source[:1500]
    
    return f"Subject: {record['subject']} | From: {record['from_name'] or ''} <{record['from_address']}> | Date: {date_part} | Body: {body}"


class Embedder:
    def __init__(self, batch_size: int = EMBEDDING_BATCH_SIZE):
        device = resolve_embedding_device()
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL_NAME} on {device}")
        self.model = SentenceTransformer(EMBEDDING_MODEL_NAME, device=device)
        self.batch_size = batch_size
        logger.info(f"Embedding model loaded (batch_size={batch_size})")
    
    def encode_batch(self, texts: list[str]) -> list[bytes]:
        embeddings = self.model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True
        )
        
        result = []
        for emb in embeddings:
            result.append(emb.astype('float32').tobytes())
        
        return result


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        pass
    return conn


from contextlib import contextmanager

@contextmanager
def get_db_connection_ctx(db_path: Path):
    """Get a DB connection with automatic cleanup. Usage: 'with get_db_connection_ctx(DB_PATH) as conn:'"""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        pass
    try:
        yield conn
    finally:
        conn.close()


def collect_email_files(maildir_path: Path) -> list[tuple[Path, str]]:
    eml_files = []
    
    for root, dirs, files in os.walk(maildir_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        folder = "Archive"
        
        for file in files:
            if file.endswith('.eml') or (not file.startswith('.') and not file.endswith('.msf') and not file.endswith('.dat')):
                full_path = Path(root) / file
                eml_files.append((full_path, folder))
    
    return sorted(eml_files, key=lambda x: str(x[0]))


def detect_mailbox_owner(conn: sqlite3.Connection) -> Optional[str]:
    """Detect the most frequent sender as mailbox owner (for is_outbound)."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT from_address FROM emails
        WHERE from_address IS NOT NULL AND from_address != ''
        GROUP BY from_address ORDER BY COUNT(*) DESC LIMIT 1
    """)
    row = cursor.fetchone()
    return row[0] if row else None


def insert_email(conn: sqlite3.Connection, record: dict):
    cursor = conn.cursor()
    
    embedding_blob = record.get('embedding')
    source = record.get('source', 'original')
    parent_id = record.get('parent_id')
    content_hash = record.get('content_hash')
    
    cursor.execute("""
        INSERT INTO emails (
            id, message_id, thread_id, subject_thread_key, timestamp,
            from_address, from_name, to_addresses, cc_addresses,
            subject, body_markdown, body_plain, body_text, body_main_text, x_mailer,
            has_attachments, attachments, folder, raw_eml, embedding,
            source, parent_id, content_hash, is_outbound,
            category_tags, sender, recipients, project_tags
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record['id'],
        record['message_id'],
        record['thread_id'],
        record['subject_thread_key'],
        record['timestamp'],
        record['from_address'],
        record['from_name'],
        record['to_addresses'],
        record['cc_addresses'],
        record['subject'],
        record['body_markdown'],
        record['body_plain'],
        record.get('body_text', ''),
        record.get('body_main_text', record.get('body_text', '')),
        record['x_mailer'],
        record['has_attachments'],
        record['attachments'],
        record['folder'],
        record['raw_eml'],
        embedding_blob,
        source,
        parent_id,
        content_hash,
        record.get('is_outbound', 0),
        record.get('category_tags'),
        record.get('sender'),
        record.get('recipients'),
        record.get('project_tags', '[]')
    ))
    
    if embedding_blob:
        try:
            cursor.execute(
                "INSERT INTO email_vectors (email_id, embedding) VALUES (?, ?)",
                (record['id'], embedding_blob)
            )
        except Exception as e:
            logger.warning(f"Could not insert vector: {e}")


def ingest_emails(maildir_path: Path, resume: bool = True, concurrent_limit: int = 4, generate_embeddings: bool = True, embedding_batch_size: int = EMBEDDING_BATCH_SIZE):
    init_database(DB_PATH)
    
    checkpoint = load_checkpoint()
    
    email_files = collect_email_files(maildir_path)
    total_files = len(email_files)
    
    logger.info(f"Found {total_files} email files")
    
    start_count = checkpoint.get('processed_count', 0)
    if resume and start_count > 0:
        last_path = checkpoint.get('last_processed_path')
        if last_path:
            for i, (path, folder) in enumerate(email_files):
                if str(path) == last_path:
                    email_files = email_files[i + 1:]
                    logger.info(f"Resuming from file {i + 1}/{total_files}")
                    break
            else:
                logger.warning("Last processed file not found, starting fresh")
    else:
        logger.info("Starting fresh ingestion")
    
    if not email_files:
        logger.info("No emails to process")
        return
    
    embedder = None
    if generate_embeddings:
        embedder = Embedder(batch_size=embedding_batch_size)
    else:
        logger.info("Skipping embedding generation (--no-embeddings)")
    
    processed_count = checkpoint.get('processed_count', 0)
    last_processed_path = checkpoint.get('last_processed_path', '')
    errors = checkpoint.get('errors', [])
    
    state_lock = threading.Lock()
    
    owner_addresses = set()
    from mcp_server.config import Config
    if Config.USER_EMAIL_ADDRESSES:
        owner_addresses = set(Config.USER_EMAIL_ADDRESSES)
    else:
        with get_db_connection_ctx(DB_PATH) as conn:
            detected_owner = detect_mailbox_owner(conn)
            if detected_owner:
                owner_addresses = {detected_owner}
                logger.info(f"Auto-detected mailbox owner: {detected_owner}")
    
    def parse_worker(eml_path: Path, folder: str):
        """Worker function: parse email with its own DB connection."""
        try:
            with get_db_connection_ctx(DB_PATH) as conn:
                records = parse_email_file(eml_path, folder, conn, source_path=str(eml_path))
                if not records:
                    return ('error', eml_path, 'ParseError', 'Failed to parse email')
                
                original_record = records[0]
                if is_duplicate_message_id(conn, original_record['message_id']):
                    return ('duplicate', eml_path, original_record['message_id'])
                
                if owner_addresses:
                    for record in records:
                        record['is_outbound'] = 1 if record['from_address'].lower() in {a.lower() for a in owner_addresses} else 0
                
                return ('success', eml_path, records)
        except Exception as e:
            return ('error', eml_path, type(e).__name__, str(e))
    
    pending_records = []
    pending_texts = []
    stopped = False
    
    with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
        future_to_path = {}
        
        for eml_path, folder in email_files:
            future = executor.submit(parse_worker, eml_path, folder)
            future_to_path[future] = eml_path
        
        for future in as_completed(future_to_path):
            eml_path = future_to_path[future]
            
            if stopped:
                continue
            
            try:
                result = future.result()
                result_type = result[0]
                
                if result_type == 'duplicate':
                    with state_lock:
                        processed_count += 1
                        last_processed_path = str(eml_path)
                    continue
                
                if result_type == 'error':
                    _, _, error_type, error_msg = result
                    with state_lock:
                        errors.append({
                            'file_path': str(eml_path),
                            'error_type': error_type,
                            'error_message': error_msg,
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        })
                    continue
                
                _, _, records = result
                
                for record in records:
                    if generate_embeddings:
                        text = generate_embedding_text(record)
                        pending_texts.append(text)
                    pending_records.append(record)
                
                if len(pending_records) >= (embedding_batch_size if generate_embeddings else 100):
                    if generate_embeddings and embedder:
                        embeddings = embedder.encode_batch(pending_texts)
                        for rec, emb in zip(pending_records, embeddings):
                            rec['embedding'] = emb
                    
                    with get_db_connection_ctx(DB_PATH) as conn:
                        for rec in pending_records:
                            insert_email(conn, rec)
                        conn.commit()
                    
                    with state_lock:
                        for rec in pending_records:
                            processed_count += 1
                            last_processed_path = rec.get('source_path', str(eml_path))
                        
                        if processed_count >= MAX_EMAILS:
                            logger.info(f"Reached limit of {MAX_EMAILS} emails, stopping")
                            stopped = True
                    
                    logger.info(f"Processed {processed_count}/{total_files} emails (including salvaged quotes)")
                    
                    pending_records = []
                    pending_texts = []
                
                with state_lock:
                    should_save = processed_count > 0 and processed_count % CHECKPOINT_INTERVAL == 0
                
                if should_save:
                    checkpoint['processed_count'] = processed_count
                    checkpoint['last_processed_path'] = last_processed_path
                    checkpoint['errors'] = errors[:]
                    save_checkpoint(checkpoint)
            
            except Exception as e:
                logger.error(f"Error processing {eml_path}: {e}")
                with state_lock:
                    errors.append({
                        'file_path': str(eml_path),
                        'error_type': type(e).__name__,
                        'error_message': str(e),
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
    
    if pending_records:
        if generate_embeddings and embedder:
            embeddings = embedder.encode_batch(pending_texts)
            for rec, emb in zip(pending_records, embeddings):
                rec['embedding'] = emb
        
        with get_db_connection_ctx(DB_PATH) as conn:
            for rec in pending_records:
                insert_email(conn, rec)
            conn.commit()
        
        with state_lock:
            for rec in pending_records:
                processed_count += 1
                last_processed_path = rec.get('source_path', '')
    
    checkpoint['processed_count'] = processed_count
    checkpoint['last_processed_path'] = last_processed_path
    checkpoint['completed_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint['errors'] = errors
    save_checkpoint(checkpoint)
    
    logger.info(f"Ingestion complete: {processed_count} emails processed")
    
    if errors:
        error_types = {}
        for e in errors:
            error_types[e['error_type']] = error_types.get(e['error_type'], 0) + 1
        logger.info(f"Total errors: {len(errors)}")
        logger.info(f"Error types: {error_types}")


def run_backfill(backfill_type: str = "all"):
    """Run data backfill operations on existing database.
    
    Args:
        backfill_type: "all" or specific operation (is_outbound, categories, projects, etc.)
    """
    from mcp_server.config import Config
    
    conn = sqlite3.connect(Config.DB_PATH)
    cursor = conn.cursor()
    
    updated = 0
    
    if backfill_type in ("all", "sender"):
        cursor.execute("""
            UPDATE emails 
            SET sender = from_address,
                body_text = COALESCE(body_text, body_markdown),
                source = COALESCE(source, 'original')
            WHERE sender IS NULL OR sender = ''
        """)
        print(f"Backfilled sender/body_text for {cursor.rowcount} rows")
        updated += cursor.rowcount
    
    if backfill_type in ("all", "from_name"):
        cursor.execute("SELECT id, from_address FROM emails WHERE from_name IS NULL AND from_address IS NOT NULL")
        rows = cursor.fetchall()
        count = 0
        for email_id, from_address in rows:
            try:
                local_part = from_address.split("@")[0]
                derived_name = local_part.replace(".", " ").replace("_", " ").title()
                cursor.execute("UPDATE emails SET from_name = ? WHERE id = ?", (derived_name, email_id))
                count += 1
            except Exception:
                continue
        conn.commit()
        print(f"Backfilled from_name for {count} rows")
        updated += count
    
    if backfill_type in ("all", "thread_id"):
        import hashlib
        cursor.execute("SELECT id, subject_thread_key FROM emails WHERE thread_id IS NULL")
        rows = cursor.fetchall()
        count = 0
        for email_id, subject_key in rows:
            try:
                if subject_key and subject_key != 'no-subject':
                    subject_hash = hashlib.sha256(subject_key.encode()).hexdigest()[:16]
                    thread_id = f"thread-subj-{subject_hash}"
                    cursor.execute("UPDATE emails SET thread_id = ? WHERE id = ?", (thread_id, email_id))
                    count += 1
            except Exception:
                continue
        conn.commit()
        print(f"Backfilled thread_id for {count} rows")
        updated += count
    
    if backfill_type in ("all", "recipients"):
        cursor.execute("SELECT id, to_addresses, cc_addresses FROM emails WHERE recipients IS NULL OR recipients = ''")
        rows = cursor.fetchall()
        count = 0
        for email_id, to_json, cc_json in rows:
            try:
                to = json.loads(to_json or '[]')
                cc = json.loads(cc_json or '[]')
                merged = list(dict.fromkeys(to + cc))
                cursor.execute("UPDATE emails SET recipients = ? WHERE id = ?", (json.dumps(merged), email_id))
                count += 1
            except Exception:
                continue
        conn.commit()
        print(f"Backfilled recipients for {count} rows")
        updated += count
    
    if backfill_type in ("all", "is_outbound"):
        cursor.execute("""
            SELECT from_address FROM emails
            WHERE from_address IS NOT NULL AND from_address != ''
            GROUP BY from_address ORDER BY COUNT(*) DESC LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            owner = row[0]
            cursor.execute("""
                UPDATE emails SET is_outbound = CASE
                    WHEN from_address = ? THEN 1 ELSE 0 END
                WHERE is_outbound IS NULL
            """, (owner,))
            print(f"Backfilled is_outbound for {cursor.rowcount} rows (owner: {owner})")
            updated += cursor.rowcount
    
    if backfill_type in ("all", "categories"):
        cursor.execute("SELECT id, subject, body_markdown FROM emails WHERE category_tags IS NULL OR category_tags = ''")
        rows = cursor.fetchall()
        count = 0
        for email_id, subject, body_markdown in rows:
            tags = classify_email({"subject": subject, "body_markdown": body_markdown or ""})
            cursor.execute("UPDATE emails SET category_tags = ? WHERE id = ?", (json.dumps(tags), email_id))
            count += 1
        conn.commit()
        print(f"Backfilled category_tags for {count} rows")
        updated += count
    
    if backfill_type in ("all", "projects"):
        cursor.execute("SELECT name, aliases FROM project_registry")
        projects = cursor.fetchall()
        
        if projects:
            cursor.execute("SELECT id, subject, body_markdown FROM emails WHERE project_tags IS NULL OR project_tags = '[]' OR project_tags = ''")
            rows = cursor.fetchall()
            count = 0
            for email_id, subject, body_markdown in rows:
                text = f"{subject or ''} {(body_markdown or '')[:2000]}".lower()
                matched = set()
                
                for name, aliases_raw in projects:
                    if name.lower() in text:
                        matched.add(name)
                        continue
                    if aliases_raw:
                        try:
                            aliases = json.loads(aliases_raw)
                        except:
                            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
                        for alias in aliases:
                            if alias.lower() in text:
                                matched.add(name)
                                break
                
                cr_matches = re.findall(r'CR\d+', subject or '')
                matched.update(cr_matches)
                
                cursor.execute("UPDATE emails SET project_tags = ? WHERE id = ?", (json.dumps(sorted(list(matched))), email_id))
                count += 1
            conn.commit()
            print(f"Backfilled project_tags for {count} rows")
            updated += count
        else:
            print("No projects in registry, skipping project_tags backfill")

    if backfill_type in ("all", "embeddings"):
        manage_embeddings(mode="missing")
        updated += 1
    
    conn.close()
    print(f"\nBackfill complete: {updated} rows updated")


def manage_embeddings(mode: str = "missing", batch_size: int = EMBEDDING_BATCH_SIZE):
    """
    Generate embeddings for emails in the database.
    
    Args:
        mode: "missing" (backfill only) or "all" (replace all)
    """
    logger.info(f"Starting embedding management in mode: {mode}")
    
    with get_db_connection_ctx(DB_PATH) as conn:
        cursor = conn.cursor()
        
        if mode == "all":
            logger.info("Clearing existing embeddings...")
            cursor.execute("UPDATE emails SET embedding = NULL")
            try:
                cursor.execute("DELETE FROM email_vectors")
            except Exception as e:
                logger.warning(f"Could not clear email_vectors: {e}")
            conn.commit()
        
        # Select records that need embeddings
        query = """
            SELECT id, subject, from_name, from_address, timestamp, body_markdown 
            FROM emails 
            WHERE embedding IS NULL
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
    if not rows:
        logger.info("No emails found that need embeddings.")
        return

    logger.info(f"Found {len(rows)} emails to embed.")
    
    embedder = Embedder(batch_size=batch_size)
    
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        batch_records = []
        batch_texts = []
        
        for row in batch:
            record = {
                'id': row[0],
                'subject': row[1],
                'from_name': row[2],
                'from_address': row[3],
                'timestamp': row[4],
                'body_markdown': row[5]
            }
            batch_records.append(record)
            batch_texts.append(generate_embedding_text(record))
        
        try:
            embeddings = embedder.encode_batch(batch_texts)
            
            with get_db_connection_ctx(DB_PATH) as conn:
                cursor = conn.cursor()
                for record, emb in zip(batch_records, embeddings):
                    cursor.execute("UPDATE emails SET embedding = ? WHERE id = ?", (emb, record['id']))
                    try:
                        cursor.execute(
                            "INSERT INTO email_vectors (email_id, embedding) VALUES (?, ?)",
                            (record['id'], emb)
                        )
                    except Exception:
                        # Fallback for duplicates or other vector table issues
                        cursor.execute("DELETE FROM email_vectors WHERE email_id = ?", (record['id'],))
                        cursor.execute(
                            "INSERT INTO email_vectors (email_id, embedding) VALUES (?, ?)",
                            (record['id'], emb)
                        )
                conn.commit()
            
            if (i + len(batch)) % 100 == 0 or (i + len(batch)) == len(rows):
                logger.info(f"Embedded {i + len(batch)}/{len(rows)} emails")
                
        except Exception as e:
            logger.error(f"Error processing batch starting at {i}: {e}")
            continue

    logger.info("Embedding management complete.")


BANNER = (
    "███████╗███╗   ███╗ █████╗ ██╗██╗     ██╗███╗   ██╗██████╗ ███████╗██╗  ██╗\n"
    "██╔════╝████╗ ████║██╔══██╗██║██║     ██║████╗  ██║██╔══██╗██╔════╝╚██╗██╔╝\n"
    "█████╗  ██╔████╔██║███████║██║██║     ██║██╔██╗ ██║██║  ██║█████╗   ╚███╔╝ \n"
    "██╔══╝  ██║╚██╔╝██║██╔══██║██║██║     ██║██║╚██╗██║██║  ██║██╔══╝   ██╔██╗ \n"
    "███████╗██║ ╚═╝ ██║██║  ██║██║███████╗██║██║ ╚████║██████╔╝███████╗██╔╝ ██╗\n"
    "╚══════╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝╚═╝╚═╝  ╚═══╝╚═════╝ ╚══════╝╚═╝  ╚═╝"
)


def main():
    print(BANNER)
    parser = argparse.ArgumentParser(description="Ingest emails from Maildir or run backfill")
    parser.add_argument("maildir", nargs="?", type=Path, help="Path to Maildir directory")
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, don't resume from checkpoint")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip embedding generation")
    parser.add_argument("--backfill", nargs="?", const="all", help="Run backfill on existing DB (default: all, or specify: is_outbound, categories, projects, etc.)")
    parser.add_argument("--backfill-embeddings", action="store_true", help="Generate missing embeddings for existing records")
    parser.add_argument("--re-embed", action="store_true", help="Clear and re-generate all embeddings")
    parser.add_argument(
        "--concurrent-limit",
        type=int,
        default=4,
        help="Number of parallel workers for ingestion (default: 4)"
    )
    parser.add_argument(
        "--embedding-batch-size",
        type=int,
        default=EMBEDDING_BATCH_SIZE,
        help="Embedding encode batch size (default: 64). Bigger = faster: MPS 8->488/s, 64->2004/s. "
             "Lower (e.g. 8) on low-RAM CPU-only hosts: batch 4096 on CPU uses ~4GB RAM."
    )
    
    args = parser.parse_args()
    
    if args.backfill_embeddings:
        manage_embeddings(mode="missing", batch_size=args.embedding_batch_size)
        return
        
    if args.re_embed:
        manage_embeddings(mode="all", batch_size=args.embedding_batch_size)
        return

    if args.backfill:
        from mcp_server.config import Config
        if args.backfill == "all":
            run_backfill("all")
        else:
            run_backfill(args.backfill)
        return
    
    if not args.maildir:
        parser.print_help()
        sys.exit(1)
    
    if not args.maildir.exists():
        logger.error(f"Maildir not found: {args.maildir}")
        sys.exit(1)
    
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    ingest_emails(
        args.maildir, 
        resume=not args.no_resume, 
        concurrent_limit=args.concurrent_limit,
        generate_embeddings=not args.no_embeddings,
        embedding_batch_size=args.embedding_batch_size
    )


if __name__ == "__main__":
    main()
