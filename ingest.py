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
from email.header import decode_header
from email.message import Message
import email
from dateutil import parser as date_parser
from bs4 import BeautifulSoup
from markdownify import markdownify as md
import zstandard as zstd
from sentence_transformers import SentenceTransformer

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"
ATTACHMENTS_DIR = BASE_DIR / "attachments"
CHECKPOINT_PATH = BASE_DIR / "ingestion" / "resume.json"
LOG_DIR = BASE_DIR / "ingestion" / "logs"

EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS = 384
EMBEDDING_BATCH_SIZE = 16
CHECKPOINT_INTERVAL = 500
ZSTD_COMPRESSION_LEVEL = 3
MAX_EMAILS = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"ingestion_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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
    def format_size(bytes_val: int) -> str:
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024:
                return f"{bytes_val:.1f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.1f} TB"


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


def init_database(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
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
            x_mailer TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachments TEXT,
            folder TEXT NOT NULL,
            raw_eml BLOB,
            embedding BLOB
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_timestamp ON emails(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_thread_id ON emails(thread_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_subject_thread_key ON emails(subject_thread_key)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_from_address ON emails(from_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder)")
    
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
            subject,
            body_markdown,
            content='emails',
            content_rowid='rowid'
        )
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_insert AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (NEW.rowid, NEW.subject, NEW.body_markdown);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_delete AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_markdown) VALUES('delete', OLD.rowid, OLD.subject, OLD.body_markdown);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_fts_update AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_markdown) VALUES('delete', OLD.rowid, OLD.subject, OLD.body_markdown);
            INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (NEW.rowid, NEW.subject, NEW.body_markdown);
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
                filename = part.get_param('filename', '', 'content-disposition')
            
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


def parse_email_file(eml_path: Path, folder: str = "INBOX", db_conn: Optional[sqlite3.Connection] = None, source_path: Optional[str] = None) -> Optional[dict]:
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
        
        if html_body:
            body_markdown = Converter.html_to_markdown(html_body)
        else:
            body_markdown = plain_body or ""
        
        body_markdown = body_markdown.strip()
        
        email_id = str(uuid.uuid4())
        
        attachments = process_attachments(message, email_id, thread_id, timestamp, db_conn)
        has_attachments = len(attachments) > 0
        
        return {
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
            'body_markdown': body_markdown,
            'body_plain': plain_body or None,
            'x_mailer': x_mailer,
            'has_attachments': 1 if has_attachments else 0,
            'attachments': json.dumps(attachments),
            'folder': folder,
            'raw_eml': compress_eml(raw_bytes),
            'embedding': None,
            'source_path': source_path or str(eml_path)
        }
    
    except Exception as e:
        logger.error(f"Error parsing {eml_path}: {e}")
        return None


def generate_embedding_text(record: dict) -> str:
    date_part = record['timestamp'][:10] if record['timestamp'] else ""
    body = record['body_markdown'][:1500] if record['body_markdown'] else ""
    
    return f"Subject: {record['subject']} | From: {record['from_name'] or ''} <{record['from_address']}> | Date: {date_part} | Body: {body}"


class Embedder:
    def __init__(self):
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL_NAME}")
        self.model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        logger.info("Embedding model loaded")
    
    def encode_batch(self, texts: list[str]) -> list[bytes]:
        embeddings = self.model.encode(
            texts,
            batch_size=EMBEDDING_BATCH_SIZE,
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


def insert_email(conn: sqlite3.Connection, record: dict):
    cursor = conn.cursor()
    
    embedding_blob = record.get('embedding')
    
    cursor.execute("""
        INSERT INTO emails (
            id, message_id, thread_id, subject_thread_key, timestamp,
            from_address, from_name, to_addresses, cc_addresses,
            subject, body_markdown, body_plain, x_mailer,
            has_attachments, attachments, folder, raw_eml, embedding
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        record['x_mailer'],
        record['has_attachments'],
        record['attachments'],
        record['folder'],
        record['raw_eml'],
        embedding_blob
    ))
    
    if embedding_blob:
        try:
            cursor.execute(
                "INSERT INTO email_vectors (email_id, embedding) VALUES (?, ?)",
                (record['id'], embedding_blob)
            )
        except Exception as e:
            logger.warning(f"Could not insert vector: {e}")


def ingest_emails(maildir_path: Path, resume: bool = True):
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
    
    embedder = Embedder()
    pending_records = []
    pending_texts = []
    
    conn = get_db_connection(DB_PATH)
    
    for idx, (eml_path, folder) in enumerate(email_files):
        try:
            record = parse_email_file(eml_path, folder, conn, source_path=str(eml_path))
            if record is None:
                checkpoint['errors'].append({
                    'file_path': str(eml_path),
                    'error_type': 'ParseError',
                    'error_message': 'Failed to parse email',
                    'timestamp': datetime.now(timezone.utc).isoformat()
                })
                continue
            
            if is_duplicate_message_id(conn, record['message_id']):
                logger.debug(f"Skipping duplicate: {record['message_id']}")
                checkpoint['processed_count'] += 1
                checkpoint['last_processed_path'] = str(eml_path)
                continue
            
            text = generate_embedding_text(record)
            pending_records.append(record)
            pending_texts.append(text)
            
            if len(pending_records) >= EMBEDDING_BATCH_SIZE:
                embeddings = embedder.encode_batch(pending_texts)
                
                for rec, emb in zip(pending_records, embeddings):
                    rec['embedding'] = emb
                    insert_email(conn, rec)
                
                conn.commit()
                
                for rec in pending_records:
                    checkpoint['processed_count'] += 1
                    checkpoint['last_processed_path'] = rec.get('source_path', str(eml_path))
                
                logger.info(f"Processed {checkpoint['processed_count']}/{total_files} emails")
                
                if checkpoint['processed_count'] >= MAX_EMAILS:
                    logger.info(f"Reached limit of {MAX_EMAILS} emails, stopping")
                    break
                
                pending_records = []
                pending_texts = []
            
            if checkpoint['processed_count'] > 0 and checkpoint['processed_count'] % CHECKPOINT_INTERVAL == 0:
                save_checkpoint(checkpoint)
        
        except Exception as e:
            logger.error(f"Error processing {eml_path}: {e}")
            checkpoint['errors'].append({
                'file_path': str(eml_path),
                'error_type': type(e).__name__,
                'error_message': str(e),
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            continue
    
    if pending_records:
        embeddings = embedder.encode_batch(pending_texts)
        
        for rec, emb in zip(pending_records, embeddings):
            rec['embedding'] = emb
            insert_email(conn, rec)
        
        conn.commit()
        
        for rec in pending_records:
            checkpoint['processed_count'] += 1
            checkpoint['last_processed_path'] = rec.get('source_path', '')
    
    conn.close()
    
    checkpoint['completed_at'] = datetime.now(timezone.utc).isoformat()
    save_checkpoint(checkpoint)
    
    logger.info(f"Ingestion complete: {checkpoint['processed_count']} emails processed")
    
    errors = checkpoint.get('errors', [])
    if errors:
        error_types = {}
        for e in errors:
            error_types[e['error_type']] = error_types.get(e['error_type'], 0) + 1
        logger.info(f"Total errors: {len(errors)}")
        logger.info(f"Error types: {error_types}")


def main():
    parser = argparse.ArgumentParser(description="Ingest emails from Maildir")
    parser.add_argument("maildir", type=Path, help="Path to Maildir directory")
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, don't resume from checkpoint")
    
    args = parser.parse_args()
    
    if not args.maildir.exists():
        logger.error(f"Maildir not found: {args.maildir}")
        sys.exit(1)
    
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    ingest_emails(args.maildir, resume=not args.no_resume)


if __name__ == "__main__":
    main()
