import sqlite3
import json
import threading
from pathlib import Path
from typing import Optional
from .config import Config
from .models import EmailRecord, EmailSearchResult, ConversationThread, AttachmentRecord
import zstandard as zstd


_thread_local = threading.local()


def get_connection() -> sqlite3.Connection:
    if hasattr(_thread_local, 'conn') and _thread_local.conn is not None:
        return _thread_local.conn
    
    conn = sqlite3.connect(str(Config.DB_PATH), timeout=Config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        pass
    
    _thread_local.conn = conn
    return conn


def close_connection():
    if hasattr(_thread_local, 'conn') and _thread_local.conn is not None:
        _thread_local.conn.close()
        _thread_local.conn = None


def decompress_eml(compressed_bytes: bytes) -> bytes:
    decompressor = zstd.ZstdDecompressor()
    return decompressor.decompress(compressed_bytes)


def search_emails(
    query: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_address: Optional[str] = None,
    to_address: Optional[str] = None,
    has_attachments: Optional[bool] = None,
    folder: Optional[str] = None,
    limit: int = 20,
    similar_to_email_id: Optional[str] = None
) -> list[EmailSearchResult]:
    conn = get_connection()
    cursor = conn.cursor()
    
    where_clauses = []
    params = []
    
    # Vector similarity search using sqlite-vec
    if similar_to_email_id:
        cursor.execute("SELECT embedding FROM emails WHERE id = ?", (similar_to_email_id,))
        row = cursor.fetchone()
        if row and row['embedding']:
            try:
                cursor.execute("""
                    SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address,
                           e.from_name, e.has_attachments, e.folder, e.body_markdown as snippet,
                           vec_distance_cosine(e.embedding, ?) as score
                    FROM emails e
                    WHERE e.id != ? AND e.embedding IS NOT NULL
                    ORDER BY score DESC
                    LIMIT ?
                """, (row['embedding'], similar_to_email_id, limit))
                
                results = cursor.fetchall()
                close_connection()
                
                return [
                    EmailSearchResult(
                        id=r['id'],
                        thread_id=r['thread_id'],
                        subject=r['subject'],
                        timestamp=r['timestamp'],
                        from_address=r['from_address'],
                        from_name=r['from_name'],
                        snippet=r['snippet'][:500] if r['snippet'] else "",
                        score=r['score'],
                        has_attachments=bool(r['has_attachments']),
                        folder=r['folder']
                    )
                    for r in results
                ]
            except Exception as e:
                close_connection()
                raise ValueError(f"Vector search failed: {e}")
    
    # Full-text search (with optional hybrid vector scoring)
    fts_only = False
    if query:
        fts_only = True
        where_clauses.append("e.rowid IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)")
        params.append(query)
    
    if date_from:
        where_clauses.append("e.timestamp >= ?")
        params.append(date_from)
    
    if date_to:
        where_clauses.append("e.timestamp <= ?")
        params.append(date_to)
    
    if from_address:
        where_clauses.append("e.from_address = ?")
        params.append(from_address)
    
    if to_address:
        where_clauses.append("e.to_addresses LIKE ?")
        params.append(f'%{to_address}%')
    
    if has_attachments is not None:
        where_clauses.append("e.has_attachments = ?")
        params.append(1 if has_attachments else 0)
    
    if folder:
        where_clauses.append("e.folder = ?")
        params.append(folder)
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    sql = f"""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address, 
               e.from_name, e.has_attachments, e.folder,
               e.body_markdown as snippet
        FROM emails e
        WHERE {where_sql}
        ORDER BY e.timestamp DESC
        LIMIT ?
    """
    params.append(limit)
    
    cursor.execute(sql, params)
    results = cursor.fetchall()
    close_connection()
    
    return [
        EmailSearchResult(
            id=row['id'],
            thread_id=row['thread_id'],
            subject=row['subject'],
            timestamp=row['timestamp'],
            from_address=row['from_address'],
            from_name=row['from_name'],
            snippet=row['snippet'][:500] if row['snippet'] else "",
            score=None,
            has_attachments=bool(row['has_attachments']),
            folder=row['folder']
        )
        for row in results
    ]


def get_email(email_id: str) -> Optional[EmailRecord]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
    row = cursor.fetchone()
    close_connection()
    
    if not row:
        return None
    
    row_dict = dict(row)
    
    if row_dict.get('raw_eml'):
        try:
            row_dict['raw_eml'] = bytes(row_dict['raw_eml'])
        except Exception:
            row_dict['raw_eml'] = None
    
    return EmailRecord.from_db_row(row_dict)


def get_conversation(thread_id: str) -> Optional[ConversationThread]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM emails 
        WHERE thread_id = ?
        ORDER BY timestamp ASC
    """, (thread_id,))
    
    rows = cursor.fetchall()
    
    if not rows:
        if thread_id.startswith('thread-'):
            normalized_subject = thread_id.replace('thread-', '')
            cursor.execute("""
                SELECT * FROM emails 
                WHERE subject_thread_key = ?
                ORDER BY timestamp ASC
            """, (normalized_subject,))
            rows = cursor.fetchall()
    
    if not rows:
        close_connection()
        return None
    
    emails = [EmailRecord.from_db_row(dict(row)) for row in rows]
    
    participants = set()
    for email in emails:
        participants.add(email.from_address)
        participants.update(email.to_addresses)
        if email.cc_addresses:
            participants.update(email.cc_addresses)
    
    subject = emails[0].subject if emails else ""
    
    close_connection()
    
    return ConversationThread(
        thread_id=thread_id,
        subject=subject,
        emails=emails,
        participant_count=len(participants),
        date_range=(emails[0].timestamp, emails[-1].timestamp),
        attachment_count=sum(1 for e in emails if e.has_attachments)
    )


def find_recipient_emails(email_address: str, limit: int = 50) -> list[EmailSearchResult]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, thread_id, subject, timestamp, from_address, from_name,
               has_attachments, folder, body_markdown as snippet
        FROM emails
        WHERE from_address = ? 
           OR to_addresses LIKE ? 
           OR cc_addresses LIKE ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (email_address, f'%{email_address}%', f'%{email_address}%', limit))
    
    results = cursor.fetchall()
    close_connection()
    
    return [
        EmailSearchResult(
            id=row['id'],
            thread_id=row['thread_id'],
            subject=row['subject'],
            timestamp=row['timestamp'],
            from_address=row['from_address'],
            from_name=row['from_name'],
            snippet=row['snippet'][:500] if row['snippet'] else "",
            score=None,
            has_attachments=bool(row['has_attachments']),
            folder=row['folder']
        )
        for row in results
    ]
