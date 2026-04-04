# PR 2: Fix FTS Rank and Normalization

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the `fts.rank` SQL crash when `exact_keywords + fields` are combined, and fix the relevance score normalization so scores produce meaningful differentiation instead of all returning 1.0.

**Architecture:** Two fixes in `database.py`: (1) ensure the FTS JOIN is included whenever `exact_keywords` is set, regardless of `fields` parameter; (2) replace the broken max-rank normalization with min-max normalization.

**Tech Stack:** SQLite FTS5, Python

**Linked Issues:** [#25](https://github.com/thomasmaerz/emailindex/issues/25) (BUG-1), [#27](https://github.com/thomasmaerz/emailindex/issues/27) (BUG-3)

---

## Root Causes

**BUG-1:** `database.py:501-509` — When `fields` is set, `_build_select_columns()` builds the SELECT columns but omits the FTS JOIN. The ORDER BY at line 479 still references `fts.rank` which doesn't exist in scope.

**BUG-3:** `database.py:538-549` — The normalization `1.0 - (r / max_rank)` produces 1.0 for all results when raw ranks are similar or equal.

## Files Modified

- `mcp_server/database.py:465-549` — FTS JOIN logic + rank normalization

## Stress Test IDs

After merge:
- **Test 3.2** (exact keyword search): No crash, returns results
- **Test 7.3** (keyword + fields): No crash, returns only requested fields
- **Test 8.3** (keyword + filters): Works with all filter combinations
- **Test 9.1** (relevance scores): Scores vary meaningfully across results
- **Test 21** (keyword edge cases): Handles empty results, single results

---

### Task 1: Fix FTS JOIN to always be present when exact_keywords is set

**Files:**
- Modify: `mcp_server/database.py:465-531`

- [ ] **Step 1: Fix the order clause and JOIN logic**

The current code at lines 473-482 sets `order_clause = "ORDER BY fts.rank DESC"` when `exact_keywords` is set, but the FTS JOIN is only added in the `use_snippet` or `fields is None` branches. Fix by ensuring the JOIN and rank column are always included when `exact_keywords` is set.

Replace the SQL building section (lines 488-531) with:

```python
# Determine if we need snippet
use_snippet = snippet_only and exact_keywords

# Determine if we need FTS rank in SELECT
needs_fts_rank = bool(exact_keywords)

# Build SELECT columns
if use_snippet:
    sql = f"""
        SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
               e.from_name, e.has_attachments, e.folder,
               snippet(emails_fts, 1, '<mark>', '</mark>', '...', {snippet_length}) as snippet,
               fts.rank
        FROM emails e
        JOIN emails_fts fts ON e.rowid = fts.rowid
        WHERE {where_sql}
        {order_clause}
        LIMIT ?
    """
elif fields is not None:
    columns = _build_select_columns(fields)
    if needs_fts_rank:
        columns += ", fts.rank"
        join_clause = "FROM emails e JOIN emails_fts fts ON e.rowid = fts.rowid"
    else:
        join_clause = "FROM emails e"
    sql = f"""
        SELECT {columns}
        {join_clause}
        WHERE {where_sql}
        {order_clause}
        LIMIT ?
    """
else:
    if needs_fts_rank:
        sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
                   e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text, fts.rank
            FROM emails e
            JOIN emails_fts fts ON e.rowid = fts.rowid
            WHERE {where_sql}
            {order_clause}
            LIMIT ?
        """
    else:
        sql = f"""
            SELECT e.id, e.thread_id, e.subject, e.timestamp, e.sender as from_address,
                   e.from_name, e.category_tags, e.project_tags, e.has_attachments, e.folder,
                   COALESCE(e.body_text, e.body_markdown) as body_text
            FROM emails e
            WHERE {where_sql}
            {order_clause}
            LIMIT ?
        """
```

- [ ] **Step 2: Also fix the order_clause to not require fts.rank when sort_by=timestamp**

At lines 466-482, the current code already handles `sort_by=timestamp` by setting `use_fts_join = False`. But the default case (line 478-482) sets `ORDER BY fts.rank DESC` without ensuring the JOIN exists when `fields` is set. The fix in Step 1 handles this by always including the JOIN when `needs_fts_rank` is True.

- [ ] **Step 3: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import query_email_database

# BUG-1 fix: exact_keywords + fields should not crash
result = query_email_database(exact_keywords='meeting', fields=['id', 'subject', 'timestamp'], limit=5)
assert 'results' in result
assert all(set(r.keys()) <= {'id', 'subject', 'timestamp', 'relevance_score'} for r in result['results'])
print('BUG-1 fix verified: no crash with exact_keywords + fields')

# BUG-1 fix: snippet_only + fields should still work
result2 = query_email_database(exact_keywords='meeting', snippet_only=True, fields=['id', 'snippet'], limit=3)
assert 'results' in result2
print('BUG-1 fix verified: snippet_only + fields works')

# BUG-3 fix: scores should vary
result3 = query_email_database(exact_keywords='meeting', limit=10)
scores = [r['relevance_score'] for r in result3['results'] if r.get('relevance_score') is not None]
if len(set(scores)) > 1:
    print('BUG-3 fix verified: scores vary')
else:
    print(f'BUG-3: scores still uniform: {scores}')
"
```

---

### Task 2: Fix rank normalization to produce meaningful score spread

**Files:**
- Modify: `mcp_server/database.py:538-549`

- [ ] **Step 1: Replace rank normalization**

Replace lines 538-549 with min-max normalization:

```python
# Compute normalized relevance scores for FTS5
rank_values = []
if exact_keywords:
    raw_ranks = []
    for row in results:
        try:
            r = row["rank"]
            # FTS5 rank is negative (more negative = more relevant)
            raw_ranks.append(r if r is not None else 0)
        except (IndexError, KeyError):
            raw_ranks.append(0)
    if raw_ranks:
        min_rank = min(raw_ranks)
        max_rank = max(raw_ranks)
        rank_range = max_rank - min_rank
        if rank_range > 0:
            # Min-max normalize to 0-1 range, then invert so higher = more relevant
            rank_values = [round(1.0 - ((r - min_rank) / rank_range), 4) for r in raw_ranks]
        else:
            # All ranks equal — assign 0.5 as neutral
            rank_values = [0.5] * len(raw_ranks)
```

- [ ] **Step 2: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import query_email_database

# Test with a common keyword that should have varying match quality
result = query_email_database(exact_keywords='project', limit=20)
scores = [r['relevance_score'] for r in result['results'] if r.get('relevance_score') is not None]
print(f'Scores: {scores}')
print(f'Unique scores: {len(set(scores))}')
print(f'Min: {min(scores)}, Max: {max(scores)}')
assert len(set(scores)) > 1, 'Scores should vary'
assert all(0 <= s <= 1 for s in scores), 'Scores should be in 0-1 range'
print('Rank normalization verified')
"
```
