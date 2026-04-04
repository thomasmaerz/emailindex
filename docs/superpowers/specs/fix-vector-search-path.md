# PR 1: Fix Vector Search Path

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the missing vector search path so `semantic_query` actually performs cosine similarity search against `email_vectors` instead of silently falling back to timestamp ordering.

**Architecture:** Add a vector search code path at the top of `query_email_database()` that activates when `semantic_query` is provided and no `exact_keywords`. The path encodes the query text via SentenceTransformer, joins `email_vectors`, orders by `vec_distance_cosine`, and returns `relevance_score` in the response.

**Tech Stack:** SQLite, sqlite-vec, SentenceTransformer, Python

**Linked Issue:** [#26](https://github.com/thomasmaerz/emailindex/issues/26)

---

## Root Cause

`database.py:374-395` — The `semantic_query` parameter exists in the function signature (line 375) but is never referenced in the function body. The function has zero vector search logic. All queries fall through to FTS5 or timestamp ordering.

**Proof:** Two completely different semantic queries return identical results:
- `semantic_query="budget concerns financial problems"` → same 5 most recent emails
- `semantic_query="happy birthday celebration party"` → same 5 most recent emails

The infrastructure is ready: `email_vectors` has 2008 rows, `sqlite-vec` loads at connection time, `vec_distance_cosine` works.

## Files Modified

- `mcp_server/database.py` — Add vector search path before the FTS5 path

## Stress Test IDs

After merge, these should pass:
- **Group 4** (semantic search): Different queries return different, relevant results
- **Group 7** (relevance scoring): Semantic queries return non-1.0 relevance scores
- **Group 8** (semantic + filters): Semantic queries respect date_from, from_address, etc.
- **Group 10** (semantic edge cases): Empty vectors, model load failures handled gracefully

---

### Task 1: Add vector search path to query_email_database

**Files:**
- Modify: `mcp_server/database.py:374-395` (insert new code block after params, before FTS WHERE clauses)

- [ ] **Step 1: Insert vector search path before FTS logic**

Add this code block at line ~401 (after WHERE clause building begins, before the `exact_keywords` FTS clause):

```python
# Vector similarity search
if semantic_query and not exact_keywords:
    try:
        embedding = _encode_text_to_embedding(semantic_query)
    except Exception as e:
        close_connection()
        raise ValueError(f"Failed to encode semantic query: {e}")

    # Build WHERE clauses for vector search (same metadata filters, minus FTS)
    vec_where_clauses = []
    vec_params = [embedding]

    if date_from:
        vec_where_clauses.append("e.timestamp >= ?")
        vec_params.append(date_from)
    if date_to:
        vec_where_clauses.append("e.timestamp <= ?")
        vec_params.append(date_to)
    if from_address:
        vec_where_clauses.append("e.sender = ?")
        vec_params.append(from_address)
    if from_name:
        vec_where_clauses.append("e.from_name LIKE ?")
        vec_params.append(f"%{from_name}%")
    if to_address:
        vec_where_clauses.append("e.recipients LIKE ?")
        vec_params.append(f"%{to_address}%")
    if is_outbound is not None:
        vec_where_clauses.append("e.is_outbound = ?")
        vec_params.append(1 if is_outbound else 0)
    if has_attachments is not None:
        vec_where_clauses.append("e.has_attachments = ?")
        vec_params.append(1 if has_attachments else 0)
    if category_filter or project_filter:
        tags = []
        if category_filter:
            tags.extend([c.strip() for c in category_filter.split(",")])
        if project_filter:
            tags.extend([p.strip() for p in project_filter.split(",")])
        if tags:
            tag_conditions = " OR ".join(["e.category_tags LIKE ?" for _ in tags])
            tag_conditions += " OR " + " OR ".join(["e.project_tags LIKE ?" for _ in tags])
            vec_where_clauses.append(f"({tag_conditions})")
            vec_params.extend([f"%{t}%" for t in tags])

    vec_where_clauses.append("(e.source IS NULL OR e.source != 'quoted_reply')")

    # Cursor pagination for vector search
    if cursor:
        last_ts, last_id = _decode_cursor(cursor)
        if sort_order == "asc":
            vec_where_clauses.append("(e.timestamp > ? OR (e.timestamp = ? AND e.id > ?))")
            vec_params.extend([last_ts, last_ts, last_id])
        else:
            vec_where_clauses.append("(e.timestamp < ? OR (e.timestamp = ? AND e.id < ?))")
            vec_params.extend([last_ts, last_ts, last_id])

    vec_where_sql = " AND ".join(vec_where_clauses) if vec_where_clauses else "1=1"

    columns = _build_select_columns(fields) if fields else """
        e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
        e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
        COALESCE(e.body_text, e.body_markdown) as body_text,
        vec_distance_cosine(ev.embedding, ?) as relevance_score
    """

    # If using default columns, we already have the embedding param; adjust params
    if fields is None:
        sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
                   e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text,
                   vec_distance_cosine(ev.embedding, ?) as relevance_score
            FROM emails e
            JOIN email_vectors ev ON e.id = ev.email_id
            WHERE {vec_where_sql}
            ORDER BY relevance_score ASC
            LIMIT ?
        """
        vec_params.append(limit)
    else:
        sql = f"""
            SELECT {columns}, vec_distance_cosine(ev.embedding, ?) as relevance_score
            FROM emails e
            JOIN email_vectors ev ON e.id = ev.email_id
            WHERE {vec_where_sql}
            ORDER BY relevance_score ASC
            LIMIT ?
        """
        vec_params.append(limit)

    cursor_conn.execute(sql, vec_params)
    results = cursor_conn.fetchall()

    output = []
    thread_ids = set()
    for row in results:
        row_dict = dict(row)
        score = row_dict.get("relevance_score")
        # Invert cosine distance to a 0-1 relevance score (closer to 0 = more relevant)
        relevance = round(1.0 - score, 4) if score is not None else None

        if fields is not None:
            item = {}
            for f in fields:
                if f in EXCLUDED_FIELDS:
                    continue
                if f == "relevance_score":
                    item[f] = relevance
                elif f == "snippet":
                    if snippet_only:
                        item[f] = row_dict.get("body_text", "")[:300] if row_dict.get("body_text") else ""
                    else:
                        item[f] = row_dict.get("body_text", "")[:500] if row_dict.get("body_text") else ""
                elif f in ("category_tags", "project_tags", "recipients", "to_addresses", "cc_addresses", "attachments"):
                    val = row_dict.get(f)
                    item[f] = json.loads(val) if val else []
                elif f in row_dict:
                    item[f] = row_dict[f]
        else:
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
                "relevance_score": relevance,
            }

        output.append(item)
        if fields is None and row_dict.get("thread_id"):
            thread_ids.add(row_dict["thread_id"])

    # Handle include_full_thread for vector search
    if include_full_thread and thread_ids:
        cursor_conn.execute("""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
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
```

- [ ] **Step 2: Verify the vector path returns early**

The vector search path must `return` its response and NOT fall through to the FTS5/timestamp path. The `return response` at the end of the vector block ensures this.

- [ ] **Step 3: Verify embedding model is loaded before use**

The `_encode_text_to_embedding()` function at line 100-103 already handles lazy loading of the SentenceTransformer model. If the model fails to load, the `try/except` block raises a `ValueError` with a clear message.

- [ ] **Step 4: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import query_email_database

# Different queries should return different results
r1 = query_email_database(semantic_query='budget concerns financial', limit=5)
r2 = query_email_database(semantic_query='happy birthday party', limit=5)
ids1 = set(r['id'] for r in r1['results'])
ids2 = set(r['id'] for r in r2['results'])
assert ids1 != ids2, 'Different queries should return different results'

# Relevance scores should vary
scores = [r['relevance_score'] for r in r1['results'] if r.get('relevance_score') is not None]
assert len(set(scores)) > 1, 'Scores should vary'

print('All vector search checks passed')
"
```

Expected: `All vector search checks passed`
