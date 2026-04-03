import sqlite3
import json
import threading
from pathlib import Path
from typing import Optional
from .config import Config
from .models import EmailRecord, EmailSearchResult, ConversationThread, AttachmentRecord
import zstandard as zstd
import numpy as np


_thread_local = threading.local()

_embedding_model = None


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(Config.EMBEDDING_MODEL)
    return _embedding_model


def _encode_text_to_embedding(text: str) -> bytes:
    model = _get_embedding_model()
    embedding = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return embedding.astype(np.float32).tobytes()


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
                      AND (e.source IS NULL OR e.source != 'quoted_reply')
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
        # Sanitize query to avoid FTS5/sqlite-vec issues
        safe_query = query.strip()
        if safe_query == "*" or not safe_query:
            # Skip FTS if query is just "*" or empty
            pass
        else:
            fts_only = True
            where_clauses.append("e.rowid IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)")
            params.append(safe_query)
    
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
    
    # Exclude quoted_reply by default (unless explicitly included)
    where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")
    
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


def query_email_database(
    semantic_query: Optional[str] = None,
    exact_keywords: Optional[str] = None,
    category_filter: Optional[str] = None,
    project_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_address: Optional[str] = None,
    to_address: Optional[str] = None,
    is_outbound: Optional[bool] = None,
    has_attachments: Optional[bool] = None,
    limit: int = 10,
    include_full_thread: bool = False
) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    
    where_clauses = []
    params = []
    
    if semantic_query:
        try:
            query_embedding = _encode_text_to_embedding(semantic_query)
            
            cursor.execute("""
                SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address,
                       e.from_name, e.has_attachments, e.folder, e.body_markdown,
                       vec_distance_cosine(e.embedding, ?) as score
                FROM emails e
                WHERE e.embedding IS NOT NULL
                  AND (e.source IS NULL OR e.source != 'quoted_reply')
                ORDER BY score ASC
                LIMIT ?
            """, (query_embedding, limit * 3))
            
            vector_results = cursor.fetchall()
            
            results = []
            thread_ids = set()
            for r in vector_results[:limit]:
                results.append({
                    "id": r["id"],
                    "thread_id": r["thread_id"],
                    "subject": r["subject"],
                    "timestamp": r["timestamp"],
                    "from_address": r["from_address"],
                    "from_name": r["from_name"],
                    "snippet": r["body_markdown"][:500] if r["body_markdown"] else "",
                    "score": r["score"],
                    "has_attachments": bool(r["has_attachments"]),
                    "folder": r["folder"]
                })
                if r["thread_id"]:
                    thread_ids.add(r["thread_id"])
            
            if include_full_thread and thread_ids:
                cursor.execute("""
                    SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address,
                           e.from_name, e.has_attachments, e.folder, e.body_markdown
                    FROM emails e
                    WHERE e.thread_id IN ({})
                      AND (e.source IS NULL OR e.source != 'quoted_reply')
                    ORDER BY e.timestamp ASC
                """.format(",".join(["?"] * len(thread_ids))), list(thread_ids))
                
                thread_emails = cursor.fetchall()
                threads = {}
                for te in thread_emails:
                    tid = te["thread_id"]
                    if tid not in threads:
                        threads[tid] = []
                    threads[tid].append({
                        "id": te["id"],
                        "subject": te["subject"],
                        "timestamp": te["timestamp"],
                        "from_address": te["from_address"],
                        "from_name": te["from_name"],
                        "snippet": te["body_markdown"][:500] if te["body_markdown"] else "",
                    })
                
                close_connection()
                return {"results": results, "threads": threads}
            
            close_connection()
            return {"results": results}
        except Exception as e:
            close_connection()
            raise ValueError(f"Vector search failed: {e}")
    
    if exact_keywords:
        safe_query = exact_keywords.strip()
        if safe_query:
            where_clauses.append("e.rowid IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)")
            params.append(safe_query)
    
    if category_filter or project_filter:
        tags = []
        if category_filter:
            tags.extend([c.strip() for c in category_filter.split(",")])
        if project_filter:
            tags.extend([p.strip() for p in project_filter.split(",")])
        
        if tags:
            tag_conditions = " OR ".join(["category_tags LIKE ?" for _ in tags])
            tag_conditions += " OR " + " OR ".join(["project_tags LIKE ?" for _ in tags])
            where_clauses.append(f"({tag_conditions})")
            params.extend([f"%{t}%" for t in tags])
    
    if date_from:
        where_clauses.append("e.timestamp >= ?")
        params.append(date_from)
    
    if date_to:
        where_clauses.append("e.timestamp <= ?")
        params.append(date_to)
    
    if from_address:
        where_clauses.append("e.sender = ?")
        params.append(from_address)
    
    if to_address:
        where_clauses.append("e.recipients LIKE ?")
        params.append(f"%{to_address}%")
    
    if is_outbound is not None:
        where_clauses.append("e.is_outbound = ?")
        params.append(1 if is_outbound else 0)
    
    if has_attachments is not None:
        where_clauses.append("e.has_attachments = ?")
        params.append(1 if has_attachments else 0)
    
    # Exclude quoted_reply by default
    where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    sql = f"""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
               e.category_tags, e.project_tags, e.has_attachments, e.folder,
               COALESCE(e.body_text, e.body_markdown) as body_text
        FROM emails e
        WHERE {where_sql}
        ORDER BY e.timestamp DESC
        LIMIT ?
    """
    params.append(limit)
    
    cursor.execute(sql, params)
    results = cursor.fetchall()
    
    output = []
    thread_ids = set()
    for row in results:
        output.append({
            "id": row["id"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "timestamp": row["timestamp"],
            "from_address": row["from_address"],
            "category_tags": row["category_tags"],
            "project_tags": row["project_tags"],
            "snippet": row["body_text"][:500] if row["body_text"] else "",
            "has_attachments": bool(row["has_attachments"]),
            "folder": row["folder"]
        })
        if row["thread_id"]:
            thread_ids.add(row["thread_id"])
    
    if include_full_thread and thread_ids:
        cursor.execute("""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
                   e.body_text
            FROM emails e
            WHERE e.thread_id IN ({})
              AND (e.source IS NULL OR e.source != 'quoted_reply')
            ORDER BY e.timestamp ASC
        """.format(",".join(["?"] * len(thread_ids))), list(thread_ids))
        
        thread_emails = cursor.fetchall()
        threads = {}
        for te in thread_emails:
            tid = te["thread_id"]
            if tid not in threads:
                threads[tid] = []
            threads[tid].append({
                "id": te["id"],
                "subject": te["subject"],
                "timestamp": te["timestamp"],
                "from_address": te["from_address"],
                "snippet": te["body_text"][:500] if te["body_text"] else "",
            })
        
        close_connection()
        return {"results": output, "threads": threads}
    
    close_connection()
    return {"results": output}


def get_project_context(project_name: str, limit: int = 10) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT name, aliases, summary, created_at
        FROM project_registry
        WHERE name = ? OR aliases LIKE ?
    """, (project_name, f"%{project_name}%"))
    
    row = cursor.fetchone()
    if not row:
        close_connection()
        return None
    
    project = {
        "name": row["name"],
        "aliases": row["aliases"],
        "summary": row["summary"],
        "created_at": row["created_at"]
    }
    
    cursor.execute("""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
               e.category_tags, e.project_tags, e.has_attachments, e.folder,
               COALESCE(e.body_text, e.body_markdown) as body_text
        FROM emails e
        WHERE e.project_tags LIKE ?
          AND (e.source IS NULL OR e.source != 'quoted_reply')
        ORDER BY e.timestamp DESC
        LIMIT ?
    """, (f"%{row['name']}%", limit))
    
    results = []
    for row in cursor.fetchall():
        results.append({
            "id": row["id"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "timestamp": row["timestamp"],
            "from_address": row["from_address"],
            "category_tags": row["category_tags"],
            "project_tags": row["project_tags"],
            "snippet": row["body_text"][:500] if row["body_text"] else "",
            "has_attachments": bool(row["has_attachments"]),
            "folder": row["folder"]
        })
    
    close_connection()
    
    return {
        "project": project,
        "emails": results
    }
