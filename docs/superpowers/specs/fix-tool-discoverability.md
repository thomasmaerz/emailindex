# PR 4: Fix Tool Discoverability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all 8 MCP tools discoverable by any compliant agent, not just those that explicitly call `tools/list`. Add cross-references in tool descriptions so agents that read descriptions can find sibling tools.

**Architecture:** Modify the `tools/list` response in `server.py` to add cross-references in the `query_email_database` description field. Audit `run-mcp-server.py` to confirm all tools are registered unconditionally at startup.

**Tech Stack:** MCP protocol, Python

**Linked Issue:** [#24](https://github.com/thomasmaerz/emailindex/issues/24)

---

## Root Cause

`server.py:293-416`. The `tools/list` handler returns all 8 tools correctly, but `query_email_database`'s description (line 301) is a single line with no cross-references. An agent that reads tool descriptions has no way to discover `get_mention_timeline`, `get_contact_profile`, `get_thread_arc`, `get_thread_by_id`, `get_email_by_id`, or `list_projects`.

## Prior False Reports

BUG-6 (partial name queries return null), BUG-7 (cursor pagination broken), and BUG-8 (snippet field ignores projection) were filed as failures by an agent that couldn't discover the tools. These should be closed as invalid with a reference to this issue.

## Files Modified

- `mcp_server/server.py:299-326` — Expand `query_email_database` description
- `run-mcp-server.py` — Audit for conditional tool registration

## Stress Test IDs

After merge:
- Any agent connecting to the server should be able to discover all 8 tools without prior knowledge
- No more false failure reports for "missing" tools

---

### Task 1: Add cross-references to query_email_database description

**Files:**
- Modify: `mcp_server/server.py:301`

- [ ] **Step 1: Expand the description field**

Replace the description at line 301:

```python
# BEFORE:
"description": "Unified email search with FTS5 tag filtering, vector similarity, and metadata filters",

# AFTER:
"description": "Unified email search with FTS5 tag filtering, vector similarity, and metadata filters. Related tools: get_email_by_id (fetch specific email by ID), get_thread_by_id (fetch full conversation by thread_id), get_mention_timeline (timeline of keyword mentions), get_contact_profile (contact interaction history), get_thread_arc (conversation thread visualization), list_projects (all registered projects).",
```

- [ ] **Step 2: Audit run-mcp-server.py for conditional tool registration**

```bash
cd /Users/thomasmaerz/emailindex
grep -n "tools" run-mcp-server.py
```

Verify that `MCPServer.__init__` registers all tools unconditionally and no tool is added conditionally based on runtime state, database content, or configuration.

- [ ] **Step 3: Verify tools/list returns all 8 tools**

```bash
cd /Users/thomasmaerz/emailindex
python3 -c "
from mcp_server.server import MCPServer
import json

server = MCPServer()
response = server.handle_request({'method': 'tools/list', 'id': 1})
tools = response['result']['tools']
tool_names = [t['name'] for t in tools]
print(f'Tools ({len(tool_names)}): {tool_names}')

expected = {
    'query_email_database', 'get_project_context', 'get_email_by_id',
    'get_thread_by_id', 'list_projects', 'get_mention_timeline',
    'get_contact_profile', 'get_thread_arc'
}
assert set(tool_names) == expected, f'Missing tools: {expected - set(tool_names)}'

# Verify cross-references in query_email_database description
qed = next(t for t in tools if t['name'] == 'query_email_database')
assert 'get_email_by_id' in qed['description'], 'Missing cross-reference to get_email_by_id'
assert 'get_thread_by_id' in qed['description'], 'Missing cross-reference to get_thread_by_id'
assert 'get_mention_timeline' in qed['description'], 'Missing cross-reference to get_mention_timeline'
assert 'get_contact_profile' in qed['description'], 'Missing cross-reference to get_contact_profile'
assert 'get_thread_arc' in qed['description'], 'Missing cross-reference to get_thread_arc'
assert 'list_projects' in qed['description'], 'Missing cross-reference to list_projects'

print('Tool discoverability fix verified')
"
```
