# PR 3: Fix Contact Profile Scope and Semantic Snippet Fallback

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `get_contact_profile` to search all contact-bearing fields (not just `from_name`), and fix `snippet_only` to produce snippets for semantic-only queries.

**Architecture:** Two independent fixes in `database.py`: (1) expand the WHERE clause in `get_contact_profile` to search `from_name`, `from_address`, `to_addresses`, `cc_addresses`, and `recipients`; (2) add a fallback snippet path for semantic queries when `snippet_only=True`.

**Tech Stack:** SQLite, Python

**Linked Issues:** [#28](https://github.com/thomasmaerz/emailindex/issues/28) (BUG-4), [#29](https://github.com/thomasmaerz/emailindex/issues/29) (BUG-5)

---

## Root Causes

**BUG-4:** `database.py:485` — `use_snippet = snippet_only and exact_keywords`. When `exact_keywords` is absent, `use_snippet` is always False even if `snippet_only=True`.

**BUG-5:** `database.py:825-827` — `get_contact_profile` only searches `e.from_name LIKE '%name%'`, missing all emails where the contact appears as recipient, CC, or in the recipients JSON array.

## Files Modified

- `mcp_server/database.py:485` — snippet_only logic
- `mcp_server/database.py:554-565` — add snippet handling for semantic path
- `mcp_server/database.py:825-827` — contact profile WHERE clause

## Stress Test IDs

After merge:
- **Test 5.x** (contact profile): `get_contact_profile(name="Ron Chinn")` returns ~130 matches
- **Test 7.x** (snippet behavior): `snippet_only=True` produces snippets for all query types

---

### Task 1: Fix get_contact_profile to search all contact fields

**Files:**
- Modify: `mcp_server/database.py:825-827`

- [ ] **Step 1: Expand the WHERE clause in get_contact_profile**

Replace lines 825-827:

```python
# BEFORE:
if name:
    where_clauses.append("e.from_name LIKE ?")
    params.append(f"%{name}%")

# AFTER:
if name:
    where_clauses.append("""(
        e.from_name LIKE ?
        OR e.from_address LIKE ?
        OR e.to_addresses LIKE ?
        OR e.cc_addresses LIKE ?
        OR e.recipients LIKE ?
    )""")
    params.extend([f"%{name}%"] * 5)
```

- [ ] **Step 2: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import get_contact_profile, get_mention_timeline

# Verify contact profile now finds more matches
result = get_contact_profile(name='Ron Chinn')
assert result is not None, 'Should find Ron Chinn'
total = result['contact']['total_sent_to_you']
print(f'Ron Chinn total_sent_to_you: {total}')
assert total > 2, f'Should find more than 2 emails, got {total}'

# Cross-check with mention timeline
timeline = get_mention_timeline(keyword='Ron Chinn')
print(f'Mention timeline total: {timeline[\"total_matches\"]}')

# Test partial name matching still works
result2 = get_contact_profile(name='Ron')
assert result2 is not None, 'Should find contacts with Ron'
print(f'Ron partial match: {result2[\"contact\"][\"total_sent_to_you\"]} emails')

print('Contact profile scope fix verified')
"
```

---

### Task 2: Fix snippet_only for semantic-only queries

**Files:**
- Modify: `mcp_server/database.py:485` — change `use_snippet` condition
- Modify: `mcp_server/database.py:554-565` — add snippet handling in output loop

- [ ] **Step 1: Change use_snippet condition**

Replace line 485:

```python
# BEFORE:
use_snippet = snippet_only and exact_keywords

# AFTER:
use_snippet = snippet_only
```

- [ ] **Step 2: Add snippet generation for semantic-only paths**

In the output building section (around line 554), when `use_snippet` is True but there's no FTS snippet (because `exact_keywords` is absent), fall back to a body text prefix. Modify the snippet output block:

```python
# In the output loop, when building the item dict for use_snippet=True:
if use_snippet:
    snippet_text = row.get("snippet", "")
    if not snippet_text and row.get("body_text"):
        snippet_text = row["body_text"][:300]
    item = {
        "id": row["id"],
        "thread_id": row.get("thread_id"),
        "subject": row.get("subject"),
        "timestamp": row["timestamp"],
        "from_address": row["from_address"],
        "from_name": row.get("from_name"),
        "snippet": snippet_text,
        "has_attachments": bool(row.get("has_attachments", 0)),
        "folder": row.get("folder"),
    }
```

For the vector search path (added in PR 1), ensure `snippet_only` produces a snippet:

```python
# In the vector search output block, when snippet_only=True:
if snippet_only:
    item["snippet"] = row_dict.get("body_text", "")[:300] if row_dict.get("body_text") else ""
```

- [ ] **Step 3: Run verification**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.database import query_email_database

# Test snippet_only with semantic query (after PR 1 merges)
result = query_email_database(semantic_query='budget concerns', snippet_only=True, limit=3)
for r in result['results']:
    assert 'snippet' in r, 'snippet key should be present'
    assert len(r['snippet']) > 0, 'snippet should not be empty'
    print(f'Snippet: {r[\"snippet\"][:80]}...')

# Test snippet_only with exact_keywords (should still work)
result2 = query_email_database(exact_keywords='meeting', snippet_only=True, limit=3)
for r in result2['results']:
    assert 'snippet' in r, 'snippet key should be present'
    print(f'FTS Snippet: {r[\"snippet\"][:80]}...')

print('Snippet fallback fix verified')
"
```

Note: The semantic snippet test requires PR 1 (vector search) to be merged first.
