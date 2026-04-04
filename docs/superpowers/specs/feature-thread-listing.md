# PR 5: Feature — Thread Listing by Message Count

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `list_threads` tool that returns threads sorted by message count, enabling queries like "find the most active conversation" without requiring a known `thread_id`.

**Architecture:** New database function `list_threads()` that aggregates by `thread_id` with `COUNT(*)`, joined with `emails` for subject/participant metadata. New MCP tool handler in `server.py`. New Pydantic model for parameters.

**Tech Stack:** SQLite, Python, Pydantic, MCP protocol

**Linked Issue:** [#30](https://github.com/thomasmaerz/emailindex/issues/30)

---

## Root Cause

No tool exists to list threads. `get_thread_by_id` and `get_thread_arc` both require a known `thread_id`. There is no bulk thread listing endpoint and no `message_count` aggregate.

## Files Modified

- `mcp_server/database.py` — Add `list_threads()` function
- `mcp_server/server.py` — Add `tool_list_threads` handler + register in `self.tools`
- `mcp_server/server.py` — Add `list_threads` to `tools/list` response
- `mcp_server/models.py` — Add `ListThreadsParams` Pydantic model

## Stress Test IDs

After merge:
- **Test 4.3** (find longest thread): `list_threads(sort_by="message_count", limit=5)` returns top 5 threads by message count
- Any query asking for "most active conversation" or "longest thread" can be answered

---

### Task 1: Add ListThreadsParams model

**Files:**
- Modify: `mcp_server/models.py` (append after `ThreadArcParams`)

- [ ] **Step 1: Add the parameter model**

```python
class ListThreadsParams(BaseModel):
    model_config = ConfigDict(strict=True)

    sort_by: str = Field(default="message_count", description="Sort field: 'message_count' or 'last_activity'")
    sort_order: str = Field(default="desc", description="Sort order: 'asc' or 'desc'")
    limit: int = Field(default=20, ge=1, le=50, description="Max threads to return")
    date_from: Optional[str] = Field(default=None, description="Start date ISO 8601")
    date_to: Optional[str] = Field(default=None, description="End date ISO 8601")
    project_filter: Optional[str] = Field(default=None, description="Comma-separated project tags")
    category_filter: Optional[str] = Field(default=None, description="Comma-separated category tags")

    @field_validator('sort_by')
    @classmethod
    def validate_sort_by(cls, v: str) -> str:
        if v not in ("message_count", "last_activity"):
            raise ValueError("sort_by must be 'message_count' or 'last_activity'")
        return v

    @field_validator('sort_order')
    @classmethod
    def validate_sort_order(cls, v: str) -> str:
        if v not in ("asc", "desc"):
            raise ValueError("sort_order must be 'asc' or 'desc'")
        return v
```

---

### Task 2: Add list_threads database function

**Files:**
- Modify: `mcp_server/database.py` (append after `get_thread_arc`)

- [ ] **Step 1: Add the database function**

```python
def list_threads(
    sort_by: str = "message_count",
    sort_order: str = "desc",
    limit: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    project_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
) -> list[dict]:
    conn = get_connection()
    cursor = conn.cursor()

    where_clauses = ["e.thread_id IS NOT NULL", "(e.source IS NULL OR e.source != 'quoted_reply')"]
    params = []

    if date_from:
        where_clauses.append("e.timestamp >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("e.timestamp <= ?")
        params.append(date_to)
    if category_filter:
        for cat in [c.strip() for c in category_filter.split(",")]:
            where_clauses.append("e.category_tags LIKE ?")
            params.append(f"%{cat}%")
    if project_filter:
        for proj in [p.strip() for p in project_filter.split(",")]:
            where_clauses.append("e.project_tags LIKE ?")
            params.append(f"%{proj}%")

    where_sql = " AND ".join(where_clauses)

    order = "DESC" if sort_order == "desc" else "ASC"
    if sort_by == "message_count":
        order_clause = f"ORDER BY message_count {order}, last_activity {order}"
    else:
        order_clause = f"ORDER BY last_activity {order}, message_count {order}"

    sql = f"""
        SELECT
            e.thread_id,
            e.subject,
            COUNT(*) as message_count,
            MIN(e.timestamp) as first_message,
            MAX(e.timestamp) as last_activity,
            COUNT(DISTINCT e.sender) as participant_count,
            SUM(CASE WHEN e.has_attachments THEN 1 ELSE 0 END) as attachment_count
        FROM emails e
        WHERE {where_sql}
        GROUP BY e.thread_id
        {order_clause}
        LIMIT ?
    """
    params.append(limit)

    cursor.execute(sql, params)
    results = []
    for row in cursor.fetchall():
        results.append({
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "message_count": row["message_count"],
            "first_message": row["first_message"],
            "last_activity": row["last_activity"],
            "participant_count": row["participant_count"],
            "attachment_count": row["attachment_count"],
        })

    close_connection()
    return results
```

---

### Task 3: Add tool handler and registration

**Files:**
- Modify: `mcp_server/server.py:31-40` — register tool
- Modify: `mcp_server/server.py` — add `tool_list_threads` method
- Modify: `mcp_server/server.py:293-416` — add to `tools/list` response

- [ ] **Step 1: Import list_threads**

Add `list_threads` to the imports at line 22:

```python
from .database import (
    search_emails, get_email, get_conversation, find_recipient_emails,
    query_email_database, get_project_context, list_projects,
    get_mention_timeline, get_contact_profile, get_thread_arc, list_threads
)
```

- [ ] **Step 2: Register the tool**

Add to `self.tools` dict at line 39:

```python
"list_threads": self.tool_list_threads,
```

- [ ] **Step 3: Add the tool handler method**

```python
def tool_list_threads(self, params: dict) -> dict:
    try:
        threads_params = ListThreadsParams(**params)
    except Exception as e:
        return {"error": str(e)}

    results = list_threads(
        sort_by=threads_params.sort_by,
        sort_order=threads_params.sort_order,
        limit=threads_params.limit,
        date_from=threads_params.date_from,
        date_to=threads_params.date_to,
        project_filter=threads_params.project_filter,
        category_filter=threads_params.category_filter,
    )

    return {
        "threads": results,
        "count": len(results)
    }
```

- [ ] **Step 4: Add import for ListThreadsParams**

Add `ListThreadsParams` to the imports at line 17:

```python
from .models import (
    EmailRecord, EmailSearchResult, ConversationThread,
    SearchParams, GetEmailParams, GetConversationParams, FindRecipientParams,
    QueryEmailParams, GetProjectContextParams, ListProjectsParams,
    MentionTimelineParams, ContactProfileParams, ThreadArcParams, ListThreadsParams
)
```

- [ ] **Step 5: Add to tools/list response**

Add after the `get_thread_arc` tool definition (around line 413):

```python
{
    "name": "list_threads",
    "description": "List threads sorted by message count or last activity. Use to find the most active conversations.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "sort_by": {"type": "string", "enum": ["message_count", "last_activity"], "description": "Sort field", "default": "message_count"},
            "sort_order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order", "default": "desc"},
            "limit": {"type": "integer", "description": "Max threads to return (1-50)", "default": 20},
            "date_from": {"type": "string", "description": "Start date ISO 8601"},
            "date_to": {"type": "string", "description": "End date ISO 8601"},
            "project_filter": {"type": "string", "description": "Comma-separated project tags"},
            "category_filter": {"type": "string", "description": "Comma-separated category tags"}
        }
    }
}
```

- [ ] **Step 6: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import list_threads

# Get top 5 threads by message count
threads = list_threads(sort_by='message_count', limit=5)
print(f'Top {len(threads)} threads by message count:')
for t in threads:
    print(f'  {t[\"thread_id\"]} | {t[\"subject\"][:50]} | {t[\"message_count\"]} msgs | {t[\"participant_count\"]} participants')

assert len(threads) > 0, 'Should return threads'
assert threads[0]['message_count'] >= threads[-1]['message_count'], 'Should be sorted DESC'

# Test with date filter
threads_filtered = list_threads(sort_by='message_count', limit=5, date_from='2020-01-01', date_to='2020-12-31')
print(f'\\nTop threads in 2020: {len(threads_filtered)}')

print('\\nThread listing feature verified')
"
```
