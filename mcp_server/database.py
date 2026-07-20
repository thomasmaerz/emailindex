import sqlite3
import json
import threading
import base64
from pathlib import Path
from typing import Optional, List
from .config import Config
from .models import EmailRecord, EmailSearchResult, ConversationThread, AttachmentRecord
import zstandard as zstd
import numpy as np
from embedding_device import resolve_embedding_device


# Default field set when 'fields' is not specified — body content is opt-in
DEFAULT_FIELDS = [
    "id", "thread_id", "timestamp", "from_address", "from_name",
    "subject", "is_outbound", "has_attachments", "source",
    "category_tags", "project_tags", "relevance_score"
]

# All allowed fields for projection
ALLOWED_FIELDS = {
    "id", "thread_id", "subject_thread_key", "timestamp", "from_address",
    "from_name", "to_addresses", "cc_addresses", "recipients", "subject",
    "body_text", "body_main_text", "body_markdown", "body_plain", "has_attachments",
    "attachments", "folder", "is_outbound", "category_tags", "project_tags",
    "parent_id", "source", "content_hash", "message_id", "x_mailer",
    "relevance_score", "snippet"
}

# Fields that are NEVER returned in API responses
EXCLUDED_FIELDS = {"raw_eml", "embedding"}


def _check_has_body_main_text(cursor_conn):
    try:
        cursor_conn.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cursor_conn.fetchall()}
        return "body_main_text" in columns
    except Exception:
        return False


def _build_select_columns(fields, has_main_text=True):
    """Build SQL column list for field projection."""
    if fields is None:
        fields = DEFAULT_FIELDS

    fields = [f for f in fields if f not in EXCLUDED_FIELDS]

    column_map = {
        "id": "e.id",
        "thread_id": "e.thread_id",
        "subject_thread_key": "e.subject_thread_key",
        "timestamp": "e.timestamp",
        "from_address": "e.from_address as from_address",
        "from_name": "e.from_name",
        "to_addresses": "e.to_addresses",
        "cc_addresses": "e.cc_addresses",
        "recipients": "e.recipients",
        "subject": "e.subject",
        "body_text": "COALESCE(e.body_text, e.body_markdown) as body_text",
        "body_main_text": "COALESCE(NULLIF(e.body_main_text, ''), e.body_text, e.body_markdown) as body_main_text" if has_main_text else "COALESCE(e.body_text, e.body_markdown) as body_main_text",
        "body_markdown": "e.body_markdown",
        "body_plain": "e.body_plain",
        "has_attachments": "e.has_attachments",
        "attachments": "e.attachments",
        "folder": "e.folder",
        "is_outbound": "e.is_outbound",
        "category_tags": "e.category_tags",
        "project_tags": "e.project_tags",
        "parent_id": "e.parent_id",
        "source": "e.source",
        "content_hash": "e.content_hash",
        "message_id": "e.message_id",
        "x_mailer": "e.x_mailer",
    }

    columns = []
    for f in fields:
        if f in column_map:
            columns.append(column_map[f])

    return ", ".join(columns) if columns else "e.id"


def _main_text_select_expr(has_main_text: bool) -> str:
    if has_main_text:
        return "COALESCE(NULLIF(e.body_main_text, ''), e.body_text, e.body_markdown)"
    return "COALESCE(e.body_text, e.body_markdown)"


def _main_text_select_expr_no_alias(has_main_text: bool) -> str:
    if has_main_text:
        return "COALESCE(NULLIF(body_main_text, ''), body_text, body_markdown)"
    return "COALESCE(body_text, body_markdown)"


def _encode_cursor(timestamp: str, email_id: str) -> str:
    data = json.dumps({"ts": timestamp, "id": email_id})
    return base64.b64encode(data.encode()).decode()


def _decode_cursor(cursor: str) -> tuple:
    data = base64.b64decode(cursor.encode()).decode()
    obj = json.loads(data)
    return obj["ts"], obj["id"]


_thread_local = threading.local()

_embedding_model = None
_embedding_model_loading = False
_embedding_model_load_error = None
_embedding_model_lock = threading.Lock()


def _load_embedding_model() -> None:
    global _embedding_model, _embedding_model_loading, _embedding_model_load_error
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(
            Config.EMBEDDING_MODEL, device=resolve_embedding_device()
        )
        with _embedding_model_lock:
            _embedding_model = model
            _embedding_model_load_error = None
    except Exception as exc:
        with _embedding_model_lock:
            _embedding_model_load_error = exc
    finally:
        with _embedding_model_lock:
            _embedding_model_loading = False


def initialize_embedding_model_async() -> None:
    global _embedding_model_loading, _embedding_model_load_error
    with _embedding_model_lock:
        if _embedding_model is not None or _embedding_model_loading:
            return
        if _embedding_model_load_error is not None:
            _embedding_model_load_error = None
        _embedding_model_loading = True
        thread = threading.Thread(target=_load_embedding_model, name="embedding-model-loader", daemon=True)
        thread.start()


def _get_embedding_model():
    global _embedding_model_loading, _embedding_model_load_error
    with _embedding_model_lock:
        if _embedding_model is not None:
            return _embedding_model
        if _embedding_model_load_error is not None:
            raise RuntimeError(f"Embedding model failed to initialize: {_embedding_model_load_error}")
        if not _embedding_model_loading:
            _embedding_model_loading = True
            thread = threading.Thread(target=_load_embedding_model, name="embedding-model-loader", daemon=True)
            thread.start()

    raise RuntimeError("Embedding model is still initializing. Retry semantic query shortly.")


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


def ensure_email_category_fts(cursor: sqlite3.Cursor) -> None:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_category_fts'")
    table_exists = cursor.fetchone() is not None

    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS email_category_fts USING fts5(
            category_tags,
            project_tags,
            content='emails'
        )
    """)

    trigger_definitions = {
        "emails_ai": """
            CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
                INSERT INTO email_category_fts(rowid, category_tags, project_tags)
                VALUES (new.rowid, new.category_tags, new.project_tags);
            END
        """,
        "emails_ad": """
            CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
                INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags)
                VALUES('delete', old.rowid, old.category_tags, old.project_tags);
            END
        """,
        "emails_au": """
            CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
                INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags)
                VALUES('delete', old.rowid, old.category_tags, old.project_tags);
                INSERT INTO email_category_fts(rowid, category_tags, project_tags)
                VALUES (new.rowid, new.category_tags, new.project_tags);
            END
        """,
    }

    missing_trigger = False
    for trigger_name, trigger_sql in trigger_definitions.items():
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name = ?",
            (trigger_name,),
        )
        if cursor.fetchone() is None:
            missing_trigger = True
        cursor.execute(trigger_sql)

    if not table_exists or missing_trigger:
        cursor.execute("INSERT INTO email_category_fts(email_category_fts) VALUES('rebuild')")
    cursor.connection.commit()


def _escape_fts5_phrase(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '""')


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
    # NOTE: This query performs a brute-force full-table scan over vectorized emails.
    # sqlite-vec v0.1.8+ supports ANN indexes, but the current schema and queries
    # in this codebase do not create or use them for this path.
    # Latency scales linearly: ~81K emails ≈ 3-5s, ~500K emails ≈ 30s.
    # See https://github.com/asg017/sqlite-vec for index options and future adoption.
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
                    ORDER BY score ASC
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
    has_main_text = _check_has_body_main_text(cursor)
    select_main_text = _main_text_select_expr_no_alias(has_main_text)
    
    # Explicitly exclude raw_eml and embedding — these must never appear in MCP responses
    cursor.execute(f"""
        SELECT id, message_id, thread_id, subject_thread_key, timestamp,
               from_address, from_name, to_addresses, cc_addresses, subject,
               body_markdown, body_plain, x_mailer, has_attachments, attachments,
               folder, source, parent_id, content_hash, sender, recipients,
               COALESCE(body_text, body_markdown) as body_text,
               {select_main_text} as body_main_text,
               category_tags, project_tags, is_outbound
        FROM emails WHERE id = ?
    """, (email_id,))
    row = cursor.fetchone()
    close_connection()
    
    if not row:
        return None
    
    row_dict = dict(row)
    row_dict['raw_eml'] = None  # permanently excluded from API responses
    
    return EmailRecord.from_db_row(row_dict)


def get_conversation(thread_id: str) -> Optional[ConversationThread]:
    conn = get_connection()
    cursor = conn.cursor()
    has_main_text = _check_has_body_main_text(cursor)
    select_main_text = _main_text_select_expr_no_alias(has_main_text)

    conversation_columns = f"""
        id, message_id, thread_id, subject_thread_key, timestamp,
        from_address, from_name, to_addresses, cc_addresses, subject,
        body_markdown, body_plain,
        COALESCE(body_text, body_markdown) as body_text,
        {select_main_text} as body_main_text,
        x_mailer, has_attachments,
        attachments, folder, source, parent_id, content_hash, sender,
        recipients, category_tags, project_tags, is_outbound
    """
    
    cursor.execute(f"""
        SELECT {conversation_columns}
        FROM emails
        WHERE thread_id = ?
        ORDER BY timestamp ASC
    """, (thread_id,))
    
    rows = cursor.fetchall()
    
    if not rows:
        if thread_id.startswith('thread-'):
            normalized_subject = thread_id.replace('thread-', '')
            cursor.execute(f"""
                SELECT {conversation_columns}
                FROM emails 
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
    from_name: Optional[str] = None,
    to_address: Optional[str] = None,
    is_outbound: Optional[bool] = None,
    has_attachments: Optional[bool] = None,
    limit: int = 10,
    include_full_thread: bool = False,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    count_only: bool = False,
    fields: Optional[List[str]] = None,
    snippet_only: bool = False,
    snippet_length: int = 32,
    cursor: Optional[str] = None,
) -> dict:
    limit = max(1, min(limit, 50))
    conn = get_connection()
    cursor_conn = conn.cursor()

    def _build_tag_filter_clause(column_name: str, tags: list[str]) -> str:
        return " OR ".join([f"{column_name} LIKE ?" for _ in tags])

    where_clauses = []
    params = []
    fts_query = None
    fts_clause_index = None  # position of FTS MATCH placeholder in where_clauses

    # Build WHERE clauses
    if exact_keywords:
        safe_query = exact_keywords.strip()
        if safe_query:
            fts_clause_index = len(where_clauses)
            where_clauses.append("emails_fts MATCH ?")
            fts_query = safe_query

    if category_filter or project_filter:
        tags = []
        if category_filter:
            tags.extend([c.strip() for c in category_filter.split(",")])
        if project_filter:
            tags.extend([p.strip() for p in project_filter.split(",")])
        if tags:
            tag_conditions = _build_tag_filter_clause("e.category_tags", tags)
            tag_conditions += " OR " + _build_tag_filter_clause("e.project_tags", tags)
            where_clauses.append(f"({tag_conditions})")
            tag_params = [f"%{t}%" for t in tags]
            params.extend(tag_params * 2)

    if date_from:
        where_clauses.append("e.timestamp >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("e.timestamp <= ?")
        params.append(date_to)
    if from_address:
        where_clauses.append("e.from_address = ?")
        params.append(from_address)
    if from_name:
        where_clauses.append("e.from_name LIKE ?")
        params.append(f"%{from_name}%")
    if to_address:
        where_clauses.append("e.recipients LIKE ?")
        params.append(f"%{to_address}%")
    if is_outbound is not None:
        where_clauses.append("e.is_outbound = ?")
        params.append(1 if is_outbound else 0)
    if has_attachments is not None:
        where_clauses.append("e.has_attachments = ?")
        params.append(1 if has_attachments else 0)

    where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")

    # Cursor-based pagination (keyset pagination)
    if cursor:
        last_ts, last_id = _decode_cursor(cursor)
        if sort_order == "asc":
            where_clauses.append("(e.timestamp > ? OR (e.timestamp = ? AND e.id > ?))")
            params.extend([last_ts, last_ts, last_id])
        else:
            where_clauses.append("(e.timestamp < ? OR (e.timestamp = ? AND e.id < ?))")
            params.extend([last_ts, last_ts, last_id])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    from_clause = "FROM emails e"
    if fts_query:
        from_clause += " JOIN emails_fts ON e.rowid = emails_fts.rowid"
    query_params = ([fts_query] if fts_query else []) + list(params)

    # Handle count_only
    if count_only:
        cursor_conn.execute(f"SELECT COUNT(*) as cnt {from_clause} WHERE {where_sql}", query_params)
        count = cursor_conn.fetchone()["cnt"]
        close_connection()
        return {"count": count}

    # Determine sort order
    use_fts_join = bool(fts_query)
    
    # Determine if we need snippet (check before semantic query path)
    use_snippet = snippet_only and (bool(fts_query) or semantic_query)
    
    # Check if DB has body_main_text column
    has_main_text = _check_has_body_main_text(cursor_conn)
    
    # Handle semantic query (vector search)
    if semantic_query and not fts_query:
        try:
            query_embedding = _encode_text_to_embedding(semantic_query)
        except Exception as e:
            close_connection()
            return {"error": f"Failed to encode semantic query: {e}", "results": []}

        select_main_text = f"{_main_text_select_expr(has_main_text)} as body_main_text"

        sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                   e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text,
                   {select_main_text},
                   -- NOTE: This query performs a brute-force full-table scan over email_vectors.
                   -- sqlite-vec v0.1.8+ supports ANN indexes, but the current schema and queries
                   -- in this codebase do not create or use them for this path.
                   -- Latency scales linearly: ~81K emails ≈ 3-5s, ~500K emails ≈ 30s.
                   -- See https://github.com/asg017/sqlite-vec for index options and future adoption.
                   vec_distance_cosine(ev.embedding, ?) as relevance_score
            FROM emails e
            JOIN email_vectors ev ON e.id = ev.email_id
            WHERE {where_sql}
            ORDER BY relevance_score ASC
            LIMIT ?
        """
        semantic_params = [query_embedding] + list(params) + [limit]

        cursor_conn.execute(sql, semantic_params)
        results = cursor_conn.fetchall()
        
        output = []
        for row in results:
            row_dict = dict(row)
            body_text = row_dict.get("body_text", "")
            body_main_text = row_dict.get("body_main_text") or body_text
            item = {
                "id": row_dict["id"],
                "thread_id": row_dict.get("thread_id"),
                "timestamp": row_dict["timestamp"],
                "from_address": row_dict["from_address"],
                "from_name": row_dict.get("from_name"),
                "subject": row_dict["subject"],
                "is_outbound": bool(row_dict.get("is_outbound", 0)),
                "has_attachments": bool(row_dict.get("has_attachments", 0)),
                "source": row_dict.get("source", "original"),
                "category_tags": json.loads(row_dict["category_tags"]) if row_dict.get("category_tags") else [],
                "project_tags": json.loads(row_dict["project_tags"]) if row_dict.get("project_tags") else [],
                "relevance_score": row_dict["relevance_score"],
            }
            if use_snippet:
                item["snippet"] = body_main_text[:300] if body_main_text else ""
            output.append(item)
        
        close_connection()
        response = {"results": output}
        if output:
            last = output[-1]
            if "timestamp" in last and "id" in last:
                response["next_cursor"] = _encode_cursor(last["timestamp"], last["id"])
                response["has_more"] = len(output) == limit
            else:
                response["next_cursor"] = None
                response["has_more"] = False
        else:
            response["next_cursor"] = None
            response["has_more"] = False
        return response

    # Hybrid search: Reciprocal Rank Fusion (RRF) combining FTS5 keyword and
    # vector/semantic results when BOTH exact_keywords and semantic_query are
    # supplied. Both queries are run independently against the common non-keyword
    # WHERE filter, then their rankings are fused.
    if fts_query and semantic_query:
        try:
            query_embedding = _encode_text_to_embedding(semantic_query)
        except Exception as e:
            close_connection()
            return {"error": f"Failed to encode semantic query: {e}", "results": []}

        # Common non-keyword WHERE (exclude FTS MATCH clause for both sides;
        # the keyword side will add its own MATCH against emails_fts)
        common_where_clauses = [c for i, c in enumerate(where_clauses) if i != fts_clause_index]
        common_params = list(params)
        common_where_sql = " AND ".join(common_where_clauses) if common_where_clauses else "1=1"

        # Pool size for each retriever. Fetch more for fusion, then trim to limit.
        pool_size = max(limit, 50)
        rrf_k = 60  # standard smoothing constant
        w_vec = 0.6
        w_fts = 0.4

        select_main_text = f"{_main_text_select_expr(has_main_text)} as body_main_text"

        # Vector side: brute-force cosine distance (consistent with existing path)
        vec_sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp,
                   e.from_address as from_address, e.from_name,
                   e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text,
                   {select_main_text},
                   vec_distance_cosine(ev.embedding, ?) as vec_distance
            FROM emails e
            JOIN email_vectors ev ON e.id = ev.email_id
            WHERE {common_where_sql}
            ORDER BY vec_distance ASC
            LIMIT ?
        """
        cursor_conn.execute(vec_sql, [query_embedding] + common_params + [pool_size])
        vec_rows = cursor_conn.fetchall()
        vec_rank_by_id = {}
        vec_row_dict = {}
        for rank_idx, row in enumerate(vec_rows):
            rd = dict(row)
            eid = rd["id"]
            if eid not in vec_rank_by_id:  # keep first (lowest distance)
                vec_rank_by_id[eid] = rank_idx
                vec_row_dict[eid] = rd

        # Keyword side: FTS5 match with bm25 rank
        fts_sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp,
                   e.from_address as from_address, e.from_name,
                   e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text,
                   {select_main_text},
                   bm25(emails_fts) as fts_rank
            FROM emails e
            JOIN emails_fts ON e.rowid = emails_fts.rowid
            WHERE emails_fts MATCH ? AND ({common_where_sql})
            ORDER BY fts_rank ASC
            LIMIT ?
        """
        cursor_conn.execute(fts_sql, [fts_query] + common_params + [pool_size])
        fts_rows = cursor_conn.fetchall()
        fts_rank_by_id = {}
        fts_row_dict = {}
        for rank_idx, row in enumerate(fts_rows):
            rd = dict(row)
            eid = rd["id"]
            if eid not in fts_rank_by_id:  # keep first (lowest bm25)
                fts_rank_by_id[eid] = rank_idx
                fts_row_dict[eid] = rd

        # RRF: collect union of all email_ids from both sides, compute fused score
        all_ids = set(vec_rank_by_id) | set(fts_rank_by_id)
        fused = []
        for eid in all_ids:
            v_rank = vec_rank_by_id.get(eid)
            f_rank = fts_rank_by_id.get(eid)
            score = 0.0
            if v_rank is not None:
                score += w_vec / (rrf_k + v_rank + 1)
            if f_rank is not None:
                score += w_fts / (rrf_k + f_rank + 1)
            fused.append((eid, score, v_rank, f_rank))

        fused.sort(key=lambda x: x[1], reverse=True)
        top = fused[:limit]

        output = []
        for eid, score, v_rank, f_rank in top:
            # Prefer FTS row for snippet; fall back to vector row if email not in FTS results
            rd = fts_row_dict.get(eid) or vec_row_dict.get(eid)
            if rd is None:
                continue
            body_text = rd.get("body_text", "")
            body_main_text = rd.get("body_main_text") or body_text
            item = {
                "id": rd["id"],
                "thread_id": rd.get("thread_id"),
                "timestamp": rd["timestamp"],
                "from_address": rd["from_address"],
                "from_name": rd.get("from_name"),
                "subject": rd["subject"],
                "is_outbound": bool(rd.get("is_outbound", 0)),
                "has_attachments": bool(rd.get("has_attachments", 0)),
                "source": rd.get("source", "original"),
                "category_tags": json.loads(rd["category_tags"]) if rd.get("category_tags") else [],
                "project_tags": json.loads(rd["project_tags"]) if rd.get("project_tags") else [],
                "relevance_score": round(score, 6),
                "fts_rank": f_rank,
                "vec_rank": v_rank,
                "retrieval_method": "both" if (f_rank is not None and v_rank is not None)
                    else ("fts" if f_rank is not None else "vector"),
            }
            if use_snippet:
                item["snippet"] = body_main_text[:300] if body_main_text else ""
            output.append(item)

        close_connection()
        response = {"results": output}
        if output:
            last = output[-1]
            if "timestamp" in last and "id" in last:
                response["next_cursor"] = _encode_cursor(last["timestamp"], last["id"])
                response["has_more"] = len(output) == limit
            else:
                response["next_cursor"] = None
                response["has_more"] = False
        else:
            response["next_cursor"] = None
            response["has_more"] = False
        return response

    if sort_by == "timestamp":
        order = "ASC" if sort_order == "asc" else "DESC"
        order_clause = f"ORDER BY e.timestamp {order}"
    elif sort_by == "relevance":
        order = "ASC" if sort_order == "asc" else "DESC"
        if fts_query:
            order_clause = f"ORDER BY rank {order}"
        else:
            order_clause = f"ORDER BY e.timestamp {order}"
            use_fts_join = False
    else:
        if fts_query:
            order_clause = "ORDER BY rank DESC"
        else:
            order_clause = "ORDER BY e.timestamp DESC"
            use_fts_join = False

    # Determine if we need snippet
    use_snippet = snippet_only and (bool(fts_query) or semantic_query)

    # Build SELECT columns
    select_main_text = f"{_main_text_select_expr(has_main_text)} as body_main_text"
    if use_snippet:
        sql = f"""
                SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                       e.from_name, e.has_attachments, e.folder,
                       {select_main_text},
                       snippet(emails_fts, 1, '<mark>', '</mark>', '...', {snippet_length}) as snippet,
                       bm25(emails_fts) as rank
                {from_clause}
                WHERE {where_sql}
                {order_clause}
                LIMIT ?
        """
        use_fts_join = True
    elif fields is not None:
        columns = _build_select_columns(fields, has_main_text=has_main_text)
        needs_snippet = "snippet" in fields
        extra_columns = []
        if needs_snippet and fts_query:
            extra_columns.append(f"snippet(emails_fts, 1, '<mark>', '</mark>', '...', {snippet_length}) as snippet")
            extra_columns.append(select_main_text)
        elif needs_snippet:
            extra_columns.append(select_main_text)
            extra_columns.append("COALESCE(e.body_text, e.body_markdown) as body_text")
        projected_columns = ", ".join([c for c in [columns] + extra_columns if c])
        if use_fts_join:
            sql = f"""
                SELECT {projected_columns}, bm25(emails_fts) as rank
                {from_clause}
                WHERE {where_sql}
                {order_clause}
                LIMIT ?
            """
        else:
            sql = f"""
                SELECT {projected_columns}
                FROM emails e
                WHERE {where_sql}
                {order_clause}
                LIMIT ?
            """
    else:
        if use_fts_join:
            sql = f"""
                SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                       e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                       COALESCE(e.body_text, e.body_markdown) as body_text,
                       {select_main_text},
                       bm25(emails_fts) as rank
                {from_clause}
                WHERE {where_sql}
                {order_clause}
                LIMIT ?
            """
        else:
            sql = f"""
                SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                       e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                       COALESCE(e.body_text, e.body_markdown) as body_text,
                       {select_main_text}
                FROM emails e
                WHERE {where_sql}
                {order_clause}
                LIMIT ?
            """
    final_params = query_params + [limit]

    cursor_conn.execute(sql, final_params)
    results = cursor_conn.fetchall()

    # Compute normalized relevance scores for FTS5
    rank_values = []
    if fts_query:
        raw_ranks = []
        for row in results:
            try:
                r = row["rank"]
                raw_ranks.append(r if r is not None else 0)
            except (IndexError, KeyError):
                raw_ranks.append(0)
        if raw_ranks:
            min_rank = min(raw_ranks)
            max_rank = max(raw_ranks)
            if max_rank > min_rank:
                rank_values = [round(1.0 - (r - min_rank) / (max_rank - min_rank), 4) for r in raw_ranks]
            else:
                rank_values = [1.0] * len(raw_ranks)

    output = []
    thread_ids = set()
    for i, row in enumerate(results):
        if use_snippet:
            preferred_snippet = row["snippet"] or row["body_main_text"]
            item = {
                "id": row["id"],
                "thread_id": row["thread_id"],
                "subject": row["subject"],
                "timestamp": row["timestamp"],
                "from_address": row["from_address"],
                "from_name": row["from_name"],
                "snippet": preferred_snippet[:500] if preferred_snippet else "",
                "has_attachments": bool(row["has_attachments"]),
                "folder": row["folder"],
            }
        elif fields is not None:
            # Custom field projection
            item = {}
            row_dict = dict(row)
            for f in fields:
                if f in EXCLUDED_FIELDS:
                    continue
                if f == "relevance_score":
                    item[f] = rank_values[i] if i < len(rank_values) else None
                elif f == "snippet":
                    snippet_text = row_dict.get("snippet") or row_dict.get("body_main_text") or row_dict.get("body_text") or ""
                    item[f] = snippet_text[:500] if snippet_text else ""
                elif f in ("category_tags", "project_tags", "recipients", "to_addresses", "cc_addresses", "attachments"):
                    val = row_dict.get(f)
                    item[f] = json.loads(val) if val else []
                elif f in row_dict:
                    item[f] = row_dict[f]
        else:
            # Default minimal fields
            row_dict = dict(row)
            item = {
                "id": row_dict["id"],
                "thread_id": row_dict.get("thread_id"),
                "timestamp": row_dict["timestamp"],
                "from_address": row_dict["from_address"],
                "from_name": row_dict.get("from_name"),
                "subject": row_dict["subject"],
                "is_outbound": bool(row_dict.get("is_outbound", 0)),
                "has_attachments": bool(row_dict.get("has_attachments", 0)),
                "source": row_dict.get("source", "original"),
                "category_tags": json.loads(row_dict["category_tags"]) if row_dict.get("category_tags") else [],
                "project_tags": json.loads(row_dict["project_tags"]) if row_dict.get("project_tags") else [],
                "relevance_score": rank_values[i] if i < len(rank_values) else None,
            }

        output.append(item)
        if not use_snippet and fields is None:
            row_dict = dict(row)
            if row_dict.get("thread_id"):
                thread_ids.add(row_dict["thread_id"])

    if include_full_thread and thread_ids:
        cursor_conn.execute("""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                   e.body_text
            FROM emails e
            WHERE e.thread_id IN ({})
              AND (e.source IS NULL OR e.source != 'quoted_reply')
            ORDER BY e.timestamp ASC
        """.format(",".join(["?"] * len(thread_ids))), list(thread_ids))

        thread_emails = cursor_conn.fetchall()
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
        response = {"results": output, "threads": threads}
        if output:
            last = output[-1]
            response["next_cursor"] = _encode_cursor(last["timestamp"], last["id"])
            response["has_more"] = len(output) == limit
        else:
            response["next_cursor"] = None
            response["has_more"] = False
        return response

    close_connection()
    response = {"results": output}
    if output:
        last = output[-1]
        if "timestamp" in last and "id" in last:
            response["next_cursor"] = _encode_cursor(last["timestamp"], last["id"])
            response["has_more"] = len(output) == limit
        else:
            response["next_cursor"] = None
            response["has_more"] = False
    else:
        response["next_cursor"] = None
        response["has_more"] = False
    return response


def get_project_context(project_name: str, limit: int = 10) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    ensure_email_category_fts(cursor)
    
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
    
    fts_query = f'project_tags:"{_escape_fts5_phrase(row["name"])}"'

    cursor.execute("""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
               e.category_tags, e.project_tags, e.has_attachments, e.folder,
               COALESCE(e.body_text, e.body_markdown) as body_text
        FROM emails e
        JOIN email_category_fts ecf ON e.rowid = ecf.rowid
        WHERE email_category_fts MATCH ?
          AND (e.source IS NULL OR e.source != 'quoted_reply')
        ORDER BY e.timestamp DESC
        LIMIT ?
    """, (fts_query, limit))
    
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


def list_projects(limit: int = 20) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT name, aliases, summary, created_at
        FROM project_registry
        ORDER BY name
        LIMIT ?
    """, (limit,))
    
    results = []
    for row in cursor.fetchall():
        aliases_list = []
        if row["aliases"]:
            aliases_list = [a.strip() for a in row["aliases"].split(",") if a.strip()]
        
        results.append({
            "name": row["name"],
            "aliases": aliases_list,
            "summary": row["summary"],
            "created_at": row["created_at"]
        })
    
    close_connection()
    return results


def get_mention_timeline(
    keyword: str,
    semantic_query: Optional[str] = None,
    granularity: str = "year",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_address: Optional[str] = None,
    is_outbound: Optional[bool] = None,
) -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    from_clause = "FROM emails e"
    where_clauses = ["e.rowid IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)"]
    params: list[object] = [keyword]

    if date_from:
        where_clauses.append("e.timestamp >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("e.timestamp <= ?")
        params.append(date_to)
    if from_address:
        where_clauses.append("e.from_address = ?")
        params.append(from_address)
    if is_outbound is not None:
        where_clauses.append("e.is_outbound = ?")
        params.append(1 if is_outbound else 0)

    where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")
    where_sql = " AND ".join(where_clauses)

    cursor.execute(f"SELECT COUNT(*) as cnt {from_clause} WHERE {where_sql}", params)
    total = cursor.fetchone()["cnt"]

    cursor.execute(f"""
        SELECT MIN(e.timestamp) as first, MAX(e.timestamp) as last
        {from_clause} WHERE {where_sql}
    """, params)
    bounds = cursor.fetchone()

    if granularity == "year":
        period_expr = "strftime('%Y', e.timestamp)"
    elif granularity == "month":
        period_expr = "strftime('%Y-%m', e.timestamp)"
    elif granularity == "quarter":
        period_expr = "strftime('%Y-', e.timestamp) || ((CAST(strftime('%m', e.timestamp) AS INTEGER) - 1) / 3 + 1)"
    else:
        period_expr = "strftime('%Y', e.timestamp)"

    cursor.execute(f"""
        SELECT {period_expr} as period, COUNT(*) as count
        {from_clause}
        WHERE {where_sql}
        GROUP BY period
        ORDER BY period
    """, params)

    timeline = {}
    for row in cursor.fetchall():
        timeline[row["period"]] = row["count"]

    close_connection()

    return {
        "keyword": keyword,
        "total_matches": total,
        "first_occurrence": bounds["first"] if bounds else None,
        "last_occurrence": bounds["last"] if bounds else None,
        "timeline": timeline
    }


def get_contact_profile(
    name: Optional[str] = None,
    email_address: Optional[str] = None,
    limit: int = 10,
    include_timeline: bool = True,
) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()

    where_clauses = []
    params = []

    if name:
        where_clauses.append("(e.from_name LIKE ? OR e.sender LIKE ? OR e.recipients LIKE ? OR e.to_addresses LIKE ? OR e.cc_addresses LIKE ?)")
        params.extend([f"%{name}%", f"%{name}%", f"%{name}%", f"%{name}%", f"%{name}%"])
    if email_address:
        where_clauses.append("(e.from_address = ? OR e.recipients LIKE ?)")
        params.extend([email_address, f"%{email_address}%"])

    if not where_clauses:
        close_connection()
        return None

    where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")
    where_sql = " AND ".join(where_clauses)

    cursor.execute(f"""
        SELECT COUNT(*) as total_sent_to_you,
               MIN(e.timestamp) as first_interaction,
               MAX(e.timestamp) as last_interaction
        FROM emails e WHERE {where_sql}
    """, params)
    stats = cursor.fetchone()

    if stats["total_sent_to_you"] == 0:
        close_connection()
        return None

    cursor.execute(f"""
        SELECT DISTINCT e.sender as address FROM emails e WHERE {where_sql}
    """, params)
    addresses = [row["address"] for row in cursor.fetchall()]

    cursor.execute(f"""
        SELECT e.from_name FROM emails e WHERE {where_sql} AND e.from_name IS NOT NULL LIMIT 1
    """, params)
    name_row = cursor.fetchone()
    display_name = name_row["from_name"] if name_row else ""

    timeline = {}
    if include_timeline:
        cursor.execute(f"""
            SELECT strftime('%Y', e.timestamp) as year, COUNT(*) as count
            FROM emails e WHERE {where_sql} GROUP BY year ORDER BY year
        """, params)
        for row in cursor.fetchall():
            timeline[row["year"]] = row["count"]

    cursor.execute(f"""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
               e.from_name, e.has_attachments, e.folder,
               COALESCE(e.body_text, e.body_markdown) as body_text, e.is_outbound
        FROM emails e WHERE {where_sql} ORDER BY e.timestamp DESC LIMIT ?
    """, params + [limit])

    sample_emails = []
    for row in cursor.fetchall():
        sample_emails.append({
            "id": row["id"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "timestamp": row["timestamp"],
            "from_address": row["from_address"],
            "snippet": row["body_text"][:300] if row["body_text"] else "",
            "is_outbound": bool(row["is_outbound"]),
        })

    close_connection()

    return {
        "contact": {
            "display_name": display_name,
            "known_addresses": addresses,
            "total_sent_to_you": stats["total_sent_to_you"],
            "first_interaction": stats["first_interaction"],
            "last_interaction": stats["last_interaction"],
            "timeline_by_year": timeline,
        },
        "sample_emails": sample_emails,
    }


def get_thread_arc(
    thread_id: str,
    mode: str = "summary",
    max_messages: int = 20,
) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
               e.from_name, e.is_outbound,
               COALESCE(e.body_text, e.body_markdown) as body_text
        FROM emails e
        WHERE e.thread_id = ?
          AND (e.source IS NULL OR e.source != 'quoted_reply')
        ORDER BY e.timestamp ASC
        LIMIT ?
    """, (thread_id, max_messages))

    rows = cursor.fetchall()

    if not rows and thread_id.startswith("thread-"):
        subject_key = thread_id.replace("thread-", "")
        cursor.execute("""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.from_address as from_address,
                   e.from_name, e.is_outbound,
                   COALESCE(e.body_text, e.body_markdown) as body_text
            FROM emails e
            WHERE e.subject_thread_key = ?
              AND (e.source IS NULL OR e.source != 'quoted_reply')
            ORDER BY e.timestamp ASC
            LIMIT ?
        """, (subject_key, max_messages))
        rows = cursor.fetchall()

    if not rows:
        close_connection()
        return None

    participants = set()
    messages = []
    for row in rows:
        participants.add(row["from_address"])
        msg = {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "from": row["from_name"] or row["from_address"],
            "direction": "outbound" if row["is_outbound"] else "inbound",
        }
        if mode == "summary":
            msg["snippet"] = row["body_text"][:200] if row["body_text"] else ""
        else:
            msg["body_text"] = row["body_text"] or ""
        messages.append(msg)

    subject = rows[0]["subject"] if rows else ""
    date_range = (rows[0]["timestamp"], rows[-1]["timestamp"]) if rows else (None, None)

    close_connection()

    return {
        "thread_id": thread_id,
        "subject": subject,
        "message_count": len(messages),
        "participants": sorted(participants),
        "date_range": list(date_range),
        "messages": messages,
    }


def list_threads(
    sort_by: str = "message_count",
    sort_order: str = "desc",
    limit: int = 10,
) -> dict:
    conn = get_connection()
    cursor = conn.cursor()

    if sort_by == "message_count":
        order_expr = "COUNT(*) {order}".format(order="DESC" if sort_order == "desc" else "ASC")
    elif sort_by == "participant_count":
        order_expr = "participant_count {order}".format(order="DESC" if sort_order == "desc" else "ASC")
    elif sort_by == "last_activity":
        order_expr = "MAX(e.timestamp) {order}".format(order="DESC" if sort_order == "desc" else "ASC")
    elif sort_by == "first_activity":
        order_expr = "MIN(e.timestamp) {order}".format(order="DESC" if sort_order == "desc" else "ASC")
    else:
        order_expr = "COUNT(*) DESC"

    cursor.execute(f"""
        SELECT e.thread_id, e.subject_thread_key,
               COUNT(*) as message_count,
               COUNT(DISTINCT COALESCE(e.sender, '') || COALESCE(e.recipients, '')) as participant_count,
               MIN(e.timestamp) as first_activity,
               MAX(e.timestamp) as last_activity
        FROM emails e
        WHERE e.thread_id IS NOT NULL
          AND (e.source IS NULL OR e.source != 'quoted_reply')
        GROUP BY e.thread_id
        ORDER BY {order_expr}
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    threads = []
    for row in rows:
        cursor.execute("""
            SELECT e.id, e.subject, e.timestamp, e.from_address as from_address, e.from_name
            FROM emails e
            WHERE e.thread_id = ?
              AND (e.source IS NULL OR e.source != 'quoted_reply')
            ORDER BY e.timestamp DESC
            LIMIT 1
        """, (row["thread_id"],))
        latest = cursor.fetchone()

        threads.append({
            "thread_id": row["thread_id"],
            "subject_thread_key": row["subject_thread_key"],
            "message_count": row["message_count"],
            "participant_count": row["participant_count"],
            "first_activity": row["first_activity"],
            "last_activity": row["last_activity"],
            "latest_email": {
                "id": latest["id"] if latest else None,
                "subject": latest["subject"] if latest else None,
                "timestamp": latest["timestamp"] if latest else None,
                "from_address": latest["from_address"] if latest else None,
                "from_name": latest["from_name"] if latest else None,
            } if latest else None,
        })

    close_connection()
    return {"threads": threads, "count": len(threads)}
