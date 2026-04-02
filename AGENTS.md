# AGENTS.md - Email Intelligence System

**Project:** Email Intelligence System  
**Location:** `emailindex/` directory in the workspace  
**Last Updated:** 2026-03-30

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Directory Structure](#2-directory-structure)
3. [MCP Tools Reference](#3-mcp-tools-reference)
4. [Tool Selection Rules](#4-tool-selection-rules)
5. [Thread Reconstruction](#5-thread-reconstruction)
6. [Attachment Handling](#6-attachment-handling)
7. [Context Guidelines](#7-context-guidelines)
8. [Query Examples](#8-query-examples)
9. [Data Model Reference](#9-data-model-reference)
10. [Priority Rules](#10-priority-rules)
11. [Common Mistakes to Avoid](#11-common-mistakes-to-avoid)
12. [Testing & Validation](#12-testing--validation)

---

## 1. Project Overview

The Email Intelligence System parses personal email archives into a queryable SQLite database, exposing data via Model Context Protocol (MCP) for AI assistants. The system supports:

- **Full-text search** across email subjects and bodies
- **Semantic vector search** using embeddings (BAAI/bge-small-en-v1.5, 384 dimensions)
- **Conversation threading** based on email headers
- **Attachment management** with SHA-256 deduplication
- **Resumable ingestion** for large archives (12+ years of email)

---

## 2. Directory Structure

```
emailindex/                          # Root of the Email Intelligence System
│
├── attachments/                    # All email attachments stored on disk
│   └── {YYYY}/                    # Year (e.g., 2024)
│       └── {MM_Mon}/             # Month (e.g., 01_Jan)
│           └── {thread_id}/      # Thread-scoped directory
│               └── {filename}     # The actual attachment file
│
├── db/
│   └── emails.db                 # Main SQLite database
│
├── ingestion/
│   ├── resume.json               # Checkpoint for resumable ingestion
│   └── logs/                    # Ingestion log files
│
├── mcp_server/                   # MCP server implementation
│   ├── __init__.py
│   ├── server.py                # Core server logic
│   ├── database.py             # SQLite queries
│   ├── models.py               # Pydantic models
│   └── config.py               # Configuration
│
├── run-mcp-server.py            # MCP entry point (handles JSON-RPC 2.0)
├── run-mcp-server.sh            # Shell script wrapper
├── ingest.py                   # Main ingestion script
├── requirements.txt            # Python dependencies
│
└── tests/                       # Test suite
    ├── run_all_validations.py  # Unified test runner
    ├── validate_extraction_pipeline.py
    ├── validate_attachments.py
    └── validate_issue4.py
```

### Key Paths

| Purpose | Path |
|---------|------|
| Database | `emailindex/db/emails.db` |
| Attachments | `emailindex/attachments/` (relative) |
| MCP Server (entry point) | `emailindex/run-mcp-server.py` |
| MCP Server (core) | `emailindex/mcp_server/server.py` |
| Checkpoint | `emailindex/ingestion/resume.json` |

---

## 3. MCP Tools Reference

The MCP server exposes exactly **2 tools**. Always use the correct tool for the task.

### 3.1 query_email_database

**Purpose:** Unified email search with FTS5 tag filtering, vector similarity, and metadata filters

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `semantic_query` | string | No | Vector search text |
| `exact_keywords` | string | No | FTS5 match |
| `category_filter` | string | No | Comma-separated categories |
| `project_filter` | string | No | Comma-separated projects |
| `date_from` | string | No | Start date ISO 8601 |
| `date_to` | string | No | End date ISO 8601 |
| `from_address` | string | No | Filter by sender |
| `to_address` | string | No | Filter by recipient |
| `is_outbound` | boolean | No | Filter by direction |
| `has_attachments` | boolean | No | Filter by attachments |
| `limit` | integer | No | Max results (default: 10, max: 50) |
| `include_full_thread` | boolean | No | Return full thread (default: false) |

**Returns:** Dictionary with `results` array and optionally `threads` object

**Search Strategy:**
- If `semantic_query` provided → Vector similarity search
- If `exact_keywords` provided → Full-text search (FTS5)
- If `category_filter` or `project_filter` provided → Tag-based filtering via FTS5
- If `include_full_thread` true → Returns full thread emails grouped by thread_id

---

### 3.2 get_project_context

**Purpose:** Get project metadata and relevant emails from project registry

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_name` | string | Yes | Project name or alias |
| `limit` | integer | No | Max emails to return (default: 10, max: 50) |

**Returns:** Dictionary with:
- `project`: Project metadata (name, aliases, summary, created_at)
- `emails`: Array of email records matching the project

**Notes:**
- Returns `null` if project not found
- Searches project_registry by name or aliases

**CRITICAL:** This is the ONLY approved method for thread reconstruction.

---

### 3.4 get_project_context

**Purpose:** Get project metadata and relevant emails from project registry

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_name` | string | Yes | Project name or alias |
| `limit` | integer | No | Max emails to return (default: 10, max: 50) |

**Returns:** Dictionary with:
- `project`: Project metadata (name, aliases, summary, created_at)
- `emails`: Array of email records matching the project

**Notes:**
- Returns `null` if project not found
- Searches project_registry by name or aliases

---

## 4. Tool Selection Rules

### 4.1 Decision Matrix

| If you want to... | Use this tool |
|-------------------|--------------|
| Find emails by keyword in subject/body | `query_email_database(exact_keywords=...)` |
| Find emails by semantic similarity | `query_email_database(semantic_query=...)` |
| Filter emails by category tags | `query_email_database(category_filter=...)` |
| Filter emails by project tags | `query_email_database(project_filter=...)` |
| Get project context and emails | `get_project_context(project_name=...)` |
| Find emails in a date range | `query_email_database(date_from=..., date_to=...)` |
| Filter by sender/recipient | `query_email_database(from_address=..., to_address=...)` |
| Get full thread in results | `query_email_database(include_full_thread=true)` |

### 4.2 NEVER Do These

| Forbidden Action | Why | Correct Approach |
|-----------------|-----|------------------|
| Manually group emails by subject | Unreliable, ignores threading headers | Use `include_full_thread=true` |
| Filter by `subject_thread_key` for threads | Fallback only, not authoritative | Use `thread_id` from results |
| Load `raw_eml` into context | Too large (~10-100KB per email) | Use `body_markdown` instead |
| Use `message_id` directly | Internal format with angle brackets | Use `id` (UUIDv4) |

---

## 5. Thread Reconstruction

### 5.1 How Threading Works

Threads are built from RFC 822 headers:

1. **Primary:** `References` header contains ordered Message-ID chain
2. **Secondary:** `In-Reply-To` contains parent Message-ID
3. **Fallback:** `subject_thread_key` (normalized subject) when headers missing

```
Email A (root)     Message-ID: <msg-A>
                   References: []

Email B (reply)    Message-ID: <msg-B>  
                   References: <msg-A>
                   In-Reply-To: <msg-A>

Email C (reply)    Message-ID: <msg-C>
                   References: <msg-A> <msg-B>
                   In-Reply-To: <msg-B>

All three have thread_id = hash(<msg-A>) = "thread-abc123"
```

### 5.2 Correct Thread Reconstruction

**WRONG (don't do this):**
```
1. search_emails(query="meeting notes")
2. Extract subject_thread_key from results
3. search_emails(query=subject_thread_key)
4. Manually combine results
```

**RIGHT (always do this):**
```
1. search_emails(query="meeting notes")
2. Get thread_id from result: "thread-abc123"
3. get_conversation(thread_id="thread-abc123")  # Returns ALL emails in thread
```

### 5.3 Why `thread_id` is Authoritative

| field | source | reliability |
|-------|--------|-------------|
| `thread_id` | References/In-Reply-To headers | Authoritative - emails explicitly linked |
| `subject_thread_key` | Normalized subject | Fallback only - subject similarity, not proof |

Two emails with the same `thread_id` ARE in the same conversation.  
Two emails with the same `subject_thread_key` MIGHT be related (false positives possible).

### 5.4 Getting Thread ID from Email

To find the thread_id for an email:

```python
# Step 1: Get the email
email = get_email(email_id="550e8400-e29b-41d4-a716-446655440000")

# Step 2: Use the thread_id field
thread_id = email.thread_id  # e.g., "thread-abc123def456"

# Step 3: Get full conversation
conversation = get_conversation(thread_id=thread_id)
```

---

## 6. Attachment Handling

### 6.1 Attachment Data Structure

Each attachment in the `attachments` JSON array has:

```json
{
  "filename": "invoice.pdf",
  "path": "attachments/2024/01_Jan/thread-abc123/invoice.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 45000,
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

### 6.2 Accessing Attachments

**Path is relative to `emailindex/` directory.**

```
# If emailindex is at: /Users/me/emailindex/

# Attachment path in JSON: attachments/2024/01_Jan/thread-abc/invoice.pdf

# Full path: /Users/me/emailindex/attachments/2024/01_Jan/thread-abc/invoice.pdf
```

**In Markdown context, attachments are referenced as:**
```markdown
See the attached document: [invoice.pdf](../attachments/2024/01_Jan/thread-abc/invoice.pdf)
```

### 6.3 Attachment Deduplication

The system deduplicates attachments by SHA-256 hash. If the same file appears in multiple emails:

- Only ONE copy is stored on disk
- All emails reference the same path
- `sha256` field identifies duplicates

This means: **same content = same path** across all emails.

### 6.4 Inline Images in Body

Inline images (Content-ID / CID attachments) are converted to Markdown:

```html
<!-- Original HTML in email -->
<img src="cid:image001@01D9ABC123" alt="signature">

<!-- Converted to Markdown in body_markdown -->
![signature](../attachments/2024/01_Jan/thread-xyz/signature.png)
```

### 6.5 Attachment Section

If email has attachments, `body_markdown` ends with:

```markdown
### 📎 Attachments

- [report.pdf](../attachments/2024/01_Jan/thread-abc/report.pdf) (PDF, 45 KB)
- [image.png](../attachments/2024/01_Jan/thread-abc/image.png) (PNG, 120 KB)
```

---

## 7. Context Guidelines

### 7.1 What to Include in Context

**DO include:**
- `body_markdown` - Clean, AI-readable text
- `subject` - For email identification
- `from_address`, `from_name` - Sender info
- `to_addresses`, `cc_addresses` - Recipients
- `timestamp` - When sent
- `attachments` JSON - File inventory (don't load files)
- `thread_id` - For conversation navigation

**DO NOT include:**
- `raw_eml` - The compressed original email (too large)
- `embedding` - Binary vector data

### 7.2 Context Size Management

| Field | Typical Size | Include? |
|-------|-------------|----------|
| `body_markdown` | 2-50 KB | Yes (truncated to relevant section if needed) |
| `raw_eml` | 10-100 KB | Never |
| `attachments` array | <1 KB | Yes (list only, not files) |
| `embedding` | 1.5 KB | Never |

### 7.3 Example: Building Email Context

```python
# DON'T:
context = f"Email content: {email.raw_eml}"  # WRONG - too large

# DO:
context = f"""
Subject: {email.subject}
From: {email.from_name or email.from_address} <{email.from_address}>
To: {', '.join(email.to_addresses)}
Date: {email.timestamp}
Folder: {email.folder}

{email.body_markdown}

Attachments ({len(email.attachments)}):
{chr(10).join(f'- {a["filename"]}' for a in email.attachments)}
"""
```

---

## 8. Query Examples

### 8.1 Find Emails About a Topic

```python
# Full-text keyword search
results = query_email_database(exact_keywords="quarterly planning meeting", limit=20)

# With date filter
results = query_email_database(
    exact_keywords="invoice",
    date_from="2024-01-01",
    date_to="2024-12-31"
)

# Semantic search (vector similarity)
results = query_email_database(
    semantic_query="quarterly planning meeting",
    limit=10
)
```

### 8.2 Find Similar Emails

```python
# Find emails semantically similar to a known email
results = query_email_database(
    semantic_query="project update",
    limit=10
)
```

### 8.3 Get Full Thread

```python
# Get emails with full thread included
results = query_email_database(
    exact_keywords="project update",
    include_full_thread=True,
    limit=5
)

# Access threads from response
if "threads" in results:
    for thread_id, thread_emails in results["threads"].items():
        print(f"Thread {thread_id}: {len(thread_emails)} emails")
```

### 8.4 Filter by Category or Project

```python
# Filter by category tags
results = query_email_database(
    category_filter="work,financial",
    limit=20
)

# Filter by project tags
results = query_email_database(
    project_filter="ProjectAlpha",
    limit=20
)
```

### 8.5 Get Project Context

```python
# Get project metadata and related emails
context = get_project_context(
    project_name="ProjectAlpha",
    limit=10
)

if context:
    print(f"Project: {context['project']['name']}")
    print(f"Summary: {context['project']['summary']}")
    print(f"Emails: {len(context['emails'])}")
```

### 8.6 Complex Search

```python
# Find emails with attachments from specific sender, filtered by category
results = query_email_database(
    exact_keywords="report",
    from_address="boss@company.com",
    category_filter="work",
    has_attachments=True,
    date_from="2024-01-01",
    limit=50
)

---

## 9. Data Model Reference

### 9.1 EmailRecord (Full)

```typescript
interface EmailRecord {
  id: string;                      // UUIDv4, primary key
  message_id: string;             // RFC 822 Message-ID
  thread_id: string | null;       // From References chain
  subject_thread_key: string;      // Normalized subject

  timestamp: string;               // ISO 8601
  from_address: string;           // Sender email
  from_name: string | null;       // Sender display name
  to_addresses: string[];         // Recipients
  cc_addresses: string[] | null;  // CC recipients

  subject: string;                // Raw subject line
  body_markdown: string;          // HTML→Markdown body
  body_plain: string | null;      // Plain text fallback
  x_mailer: string | null;        // Mail client

  has_attachments: boolean;
  attachments: AttachmentRecord[]; // List of attachments

  folder: string;                 // Maildir folder
  raw_eml: Buffer | null;         // Zstd-compressed .eml
}

interface AttachmentRecord {
  filename: string;
  path: string;                   // Relative to emailindex/
  mime_type: string;
  size_bytes: number;
  sha256: string;
}

interface ConversationThread {
  thread_id: string;
  subject: string;
  emails: EmailRecord[];
  participant_count: number;
  date_range: [string, string];   // [earliest, latest]
  attachment_count: number;
}
```

### 9.2 EmailSearchResult (Limited)

```typescript
interface EmailSearchResult {
  id: string;
  thread_id: string | null;
  subject: string;
  timestamp: string;
  from_address: string;
  from_name: string | null;
  snippet: string;               // Relevant search excerpt
  score: number | null;          // Relevance score (vector search)
  has_attachments: boolean;
  folder: string;
}
```

### 9.3 SQLite Table Columns

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT | UUIDv4 primary key |
| `message_id` | TEXT | RFC 822 Message-ID (UNIQUE) |
| `thread_id` | TEXT | From References chain |
| `subject_thread_key` | TEXT | Normalized subject |
| `timestamp` | TEXT | ISO 8601 |
| `from_address` | TEXT | Sender email |
| `from_name` | TEXT | Display name |
| `to_addresses` | TEXT | JSON array |
| `cc_addresses` | TEXT | JSON array |
| `subject` | TEXT | Raw subject |
| `body_markdown` | TEXT | Markdown body |
| `body_plain` | TEXT | Plain text |
| `x_mailer` | TEXT | Mail client |
| `has_attachments` | INTEGER | 0 or 1 |
| `attachments` | TEXT | JSON array |
| `folder` | TEXT | Maildir folder |
| `raw_eml` | BLOB | Zstd-compressed |
| `embedding` | BLOB | sqlite-vec vector |

---

## 10. Priority Rules

### 10.1 Threading Priority

| Priority | Method | When to Use |
|----------|--------|-------------|
| **HIGH** | `thread_id` | Conversation reconstruction, definitive grouping |
| **LOW** | `subject_thread_key` | Fallback only when `thread_id` is null |

**Rule:** If an email has a `thread_id`, ALWAYS use it for conversation grouping. `subject_thread_key` is for edge cases only.

### 10.2 Search Priority

| Priority | Method | When to Use |
|----------|--------|-------------|
| **HIGH** | Semantic similarity | Finding conceptually related emails |
| **MEDIUM** | Full-text (FTS5) | Exact keyword matching |
| **LOW** | Metadata filters | Narrowing by date, sender, folder |

### 10.3 Body Content Priority

| Priority | Field | When to Use |
|----------|-------|-------------|
| **HIGH** | `body_markdown` | Primary content for AI context |
| **LOW** | `body_plain` | Fallback when `body_markdown` empty |

### 10.4 Attachment Priority

| Priority | Method | When to Use |
|----------|--------|-------------|
| **HIGH** | Inline images in body | Context-relevant images |
| **MEDIUM** | Attachment list in body | Reference list at bottom |
| **LOW** | `attachments` JSON | Just need filenames |

---

## 11. Common Mistakes to Avoid

### 11.1 Thread Reconstruction

**MISTAKE:** Manually grouping emails by subject
```python
# WRONG - Subject matching is unreliable
results = search_emails(query="meeting")
subjects = set(r.subject for r in results)
for subj in subjects:
    thread = [r for r in results if r.subject == subj]  # WRONG
```

**CORRECT:** Use thread_id from headers
```python
# RIGHT - Threading is header-based
results = search_emails(query="meeting")
thread_ids = set(r.thread_id for r in results if r.thread_id)
for tid in thread_ids:
    conversation = get_conversation(thread_id=tid)  # CORRECT
```

### 11.2 Context Size

**MISTAKE:** Including raw_eml in context
```python
# WRONG - raw_eml can be 100KB+
context = email.raw_eml.decode('zstd')
```

**CORRECT:** Use body_markdown
```python
# RIGHT - body_markdown is 2-50KB typically
context = email.body_markdown
```

### 11.3 Attachment Paths

**MISTAKE:** Using absolute paths
```python
# WRONG - Won't work across systems
path = "/Users/me/emailindex/attachments/..."
```

**CORRECT:** Use relative paths with adjustment
```python
# RIGHT - Attachments JSON stores relative paths
# Construct full path based on emailindex location
base = "emailindex/"
full_path = base + email.attachments[0]["path"]
```

### 11.4 Message-ID vs ID

**MISTAKE:** Using message_id as the primary key
```python
# WRONG - message_id has angle brackets, not UUID format
email = get_email(email_id="<CAFq123@example.com>")  # WRONG
```

**CORRECT:** Use id (UUIDv4)
```python
# RIGHT - id is UUIDv4 format
email = get_email(email_id="550e8400-e29b-41d4-a716-446655440000")  # RIGHT
```

### 11.5 Date Format

**MISTAKE:** Assuming flexible date parsing
```python
# May work but be explicit
search_emails(date_from="Jan 15, 2024")  # Ambiguous
```

**CORRECT:** Use ISO 8601
```python
# Unambiguous
search_emails(date_from="2024-01-15")
search_emails(date_from="2024-01-15T00:00:00Z")
```

### 11.6 Folder Names

**MISTAKE:** Assuming simple folder names
```python
# WRONG - Maildir folders often have dots
search_emails(folder="Sent Mail")  # Wrong
```

**CORRECT:** Match exact folder names from data
```python
# RIGHT - Use actual folder names
search_emails(folder="Sent")
search_emails(folder=".Drafts")
search_emails(folder=".Trash")
```

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────┐
│                    MCP TOOL CHEAT SHEET                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  FIND EMAILS BY KEYWORDS                                        │
│  ───────────────────────────                                    │
│  query_email_database(exact_keywords="quarterly report")       │
│                                                                 │
│  FIND SIMILAR EMAILS                                            │
│  ─────────────────────                                          │
│  query_email_database(semantic_query="project update")         │
│                                                                 │
│  FILTER BY CATEGORY/PROJECT                                     │
│  ─────────────────────────────                                  │
│  query_email_database(category_filter="work,financial")         │
│  query_email_database(project_filter="ProjectAlpha")           │
│                                                                 │
│  GET PROJECT CONTEXT                                            │
│  ─────────────────────────                                      │
│  get_project_context(project_name="ProjectAlpha")              │
│                                                                 │
│  GET FULL THREAD IN RESULTS                                     │
│  ───────────────────────────────                                │
│  query_email_database(include_full_thread=true)                 │
│                                                                 │
│  FILTER BY DATE                                                 │
│  ─────────────                                                  │
│  query_email_database(date_from="2024-01-01", date_to="2024-12-31")
│                                                                 │
│  FILTER BY SENDER/RECIPIENT                                     │
│  ───────────────────────────                                    │
│  query_email_database(from_address="alice@example.com")        │
│  query_email_database(to_address="bob@example.com")            │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│  NEVER DO:                                                     │
│  • raw_eml in context (too large)                               │
│  • Manual thread grouping by subject                            │
│  • message_id instead of id                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12. Testing & Validation

The Email Intelligence System includes a comprehensive validation suite that tests the entire pipeline from raw email files through to the database and vector embeddings.

### 12.1 Test Scripts

| Script | Purpose | What it validates |
|--------|---------|-------------------|
| `tests/run_all_validations.py` | **Unified runner** - runs all tests with a single command | Full pipeline coverage |
| `tests/validate_extraction_pipeline.py` | Email extraction quality | Maildir→DB field fidelity, body content, headers |
| `tests/validate_attachments.py` | Attachment pipeline | Files on disk vs DB records, orphaned files |
| `tests/validate_issue4.py` | Vector/embedding pipeline | sqlite-vec loads, embedding dimensions, similarity search |

### 12.2 Running Tests

**Quick validation (summary only):**
```bash
python tests/run_all_validations.py
```

**Detailed output:**
```bash
python tests/run_all_validations.py --verbose
```

**Machine-readable (JSON):**
```bash
python tests/run_all_validations.py --json
```

**Run specific test only:**
```bash
python tests/run_all_validations.py --filter extraction
python tests/run_all_validations.py --filter attachments
python tests/run_all_validations.py --filter vector
```

**Individual test scripts:**
```bash
python tests/validate_extraction_pipeline.py --verbose
python tests/validate_attachments.py --verbose --has-attachments-fix
python tests/validate_issue4.py --verbose
```

### 12.3 Interpreting Results

**Pass/Fail indicators:**
- `✅ PASS` - All checks in that pipeline phase passed
- `❌ FAIL` - One or more checks failed (check output for specifics)
- `⏭️ SKIP` - Test was skipped (typically missing data dependencies)
- `⚠️ ERROR` - Test couldn't run (missing database, maildir, etc.)

**Example output:**
```
Email Intelligence System - Validation Report
==============================================
Run: 2026-04-01 15:30:00
Database: /path/to/emails.db

Pipeline Phase       Status   Details
─────────────────────────────────────────────
Extraction Pipeline   ✅ PASS   48/50 valid (96%)
Attachment Pipeline   ✅ PASS   0 orphans, 0 missing
Vector Pipeline      ✅ PASS   1500/1500 (384-dim)
─────────────────────────────────────────────
Overall: ✅ PASS (3/3 checks)
```

### 12.4 Investigating Failures

**Quick investigation:**
```bash
# Re-run with verbose to see per-email details
python tests/run_all_validations.py --verbose

# Or run a specific test individually
python tests/validate_extraction_pipeline.py --verbose
python tests/validate_attachments.py --verbose
python tests/validate_issue4.py --verbose
```

**Persistent logs:**
All validation runs write log files to `ingestion/logs/validation_YYYYMMDD_HHMMSS.log`. These contain:
- Every check performed
- Actual values compared (e.g., `DB: has_attachments=0 | Maildir: 2 attachments`)
- Full error context for failures
- Timestamps for audit trail

**To find a specific email in logs:**
```bash
# Find log files
ls -la ingestion/logs/validation_*.log

# Search for specific email ID or attachment
grep -i "email-id-here" ingestion/logs/validation_*.log
grep -i "attachment-name" ingestion/logs/validation_*.log
```

### 12.5 Prerequisites

All tests require:
1. `db/emails.db` - SQLite database with email records
2. `maildir/cur/` - Original .eml files (for extraction/attachment validation)
3. `attachments/` - Directory where extracted attachments are stored

If any of these are missing, tests will report `ERROR` and skip those checks.

---

**End of AGENTS.md**
