# Email Intelligence System (emailindex)

**Email Intelligence System** is a powerful indexing and search engine for 12+ years of Maildir archives. It combines traditional full-text search with modern semantic vector search, exposed via the Model Context Protocol (MCP).

---

## Features

- **Semantic Vector Search**: True semantic similarity search using `BAAI/bge-small-en-v1.5` embeddings (384-dim) with `sqlite-vec` for efficient cosine distance ranking.
- **Full-Text Search (FTS5)**: SQLite FTS5 with BM25 ranking for exact keyword matching.
- **Intelligent Threading**: Reconstructs conversation threads using RFC 822 References/In-Reply-To chains with subject-based fallback for emails missing standard headers.
- **Deduplicated Attachments**: SHA-256 based deduplication ensures that multiple copies of the same file across different emails only occupy space once.
- **Resumable Ingestion**: Batch-based ingestion pipeline with checkpointing and error recovery.
- **Concurrency Support**: Thread-local database connections for safe parallel MCP requests.
- **Model Context Protocol (MCP)**: Exposes search and retrieval tools directly to AI assistants like Claude Desktop or OpenCode.
- **Project Context**: Tag-based email categorization with project registry for contextual AI queries.

---

## Architecture

```mermaid
graph TB
    subgraph "Ingestion Pipeline"
        A[Maildir .eml files] --> B[ingest.py]
        B --> C[Parse & Extract]
        C --> D[HTML to Markdown]
        C --> E[SHA-256 Dedup]
        C --> F[Generate Embeddings]
        F --> G[SentenceTransformer bge-small-en-v1.5]
        D --> H[emails table]
        E --> I[attachments/ on disk]
        F --> J[email_vectors table]
        H --> K[emails_fts FTS5 index]
    end

    subgraph "MCP Server"
        L[AI Assistant OpenCode / Claude] --> M[run-mcp-server.py JSON-RPC 2.0]
        M --> N[mcp_server/server.py Tool routing]
        N --> O[mcp_server/database.py Query execution]
        O --> H
        O --> J
        O --> K
    end

    subgraph "Storage"
        H
        I
        J
        K
    end
```

### System Components

| Component | File | Purpose |
|-----------|------|---------|
| MCP Entry Point | `run-mcp-server.py` | JSON-RPC 2.0 protocol wrapper, stdio transport |
| Server Logic | `mcp_server/server.py` | Tool definitions, request routing, schema validation |
| Database Layer | `mcp_server/database.py` | SQLite queries, vector search, lazy embedding model |
| Data Models | `mcp_server/models.py` | Pydantic validation (EmailRecord, SearchParams, etc.) |
| Configuration | `mcp_server/config.py` | Paths, model settings, embedding dimensions |
| Ingestion | `ingest.py` | Maildir parsing, embedding generation, storage |
| Migration | `migrate_v2.py` | Schema migration for v2 columns (tags, threading) |

---

## Tech Stack

- **Python 3.12+**
- **Conda** (Recommended for dependency management)
- **SQLite** with `fts5` and `sqlite-vec` extensions
- **Sentence-Transformers** (`BAAI/bge-small-en-v1.5`)
- **Pydantic v2** for robust data validation
- **Zstandard** for email body compression
- **BeautifulSoup4 & Markdownify** for HTML-to-Markdown conversion

---

## Getting Started

### 1. Installation (Conda Recommended)

```bash
conda create -n emailindex python=3.12 -y
conda activate emailindex
pip install -r requirements.txt
```

### 2. Ingestion

```bash
python3 ingest.py /path/to/your/maildir
```

This will:
1. Parse all `.eml` files from your Maildir
2. Generate 384-dimensional semantic embeddings
3. Compress and store raw email content with zstd
4. Deduplicate attachments to `attachments/`
5. Build FTS5 index and populate vector database

### 3. MCP Server

```bash
python3 run-mcp-server.py --stdio
```

The wrapper script handles JSON-RPC 2.0 protocol handshake (`initialize`, `initialized`) and response formatting.

---

## MCP Client Integration

### OpenCode

```json
{
  "mcp": {
    "emailindex": {
      "type": "local",
      "command": [
        "/path/to/miniconda3/envs/emailindex/bin/python3",
        "/path/to/emailindex/run-mcp-server.py"
      ],
      "enabled": true
    }
  }
}
```

### Claude Desktop

```json
{
  "mcpServers": {
    "emailindex": {
      "command": ["/path/to/miniconda3/envs/emailindex/bin/python3", "/path/to/emailindex/run-mcp-server.py"],
      "env": {
        "PYTHONPATH": "/path/to/emailindex"
      }
    }
  }
}
```

---

## MCP Tools

The server exposes **2 tools** for AI assistants:

### `query_email_database`

Unified email search with multiple strategies:

```mermaid
flowchart LR
    A[query_email_database] --> B{semantic_query?}
    B -->|yes| C[Generate embedding from query text]
    C --> D[vec_distance_cosine ORDER BY score ASC]
    D --> E[Return ranked results]

    B -->|no| F{exact_keywords?}
    F -->|yes| G[FTS5 MATCH query]
    G --> E

    F -->|no| H{category or project filter?}
    H -->|yes| I[Tag-based LIKE filter]
    I --> E

    H -->|no| J[Metadata filters date, sender, etc.]
    J --> E
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `semantic_query` | string | Vector search text - generates embedding at query time |
| `exact_keywords` | string | FTS5 exact keyword match |
| `category_filter` | string | Comma-separated category tags |
| `project_filter` | string | Comma-separated project tags |
| `date_from` | string | Start date (ISO 8601) |
| `date_to` | string | End date (ISO 8601) |
| `from_address` | string | Filter by sender |
| `to_address` | string | Filter by recipient |
| `is_outbound` | boolean | Filter by direction |
| `has_attachments` | boolean | Filter by attachments |
| `limit` | integer | Max results (1-50, default: 10) |
| `include_full_thread` | boolean | Return full conversation thread |

### `get_project_context`

Get project metadata and related emails from the project registry.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_name` | string | Project name or alias (required) |
| `limit` | integer | Max emails to return (1-50, default: 10) |

---

## Search Architecture

### Vector Search Flow

```mermaid
sequenceDiagram
    participant Client as AI Assistant
    participant Server as MCP Server
    participant DB as database.py
    participant Model as SentenceTransformer
    participant SQLite as SQLite + sqlite-vec

    Client->>Server: query_email_database(semantic_query="quarterly planning")
    Server->>DB: query_email_database()
    DB->>Model: _get_embedding_model()
    Note over Model: Lazy load on first call caches for reuse
    Model-->>DB: Loaded model
    DB->>Model: encode("quarterly planning")
    Model-->>DB: float32[384] array
    DB->>DB: .astype(float32).tobytes()
    DB->>SQLite: vec_distance_cosine(embedding, ?) ORDER BY score ASC
    SQLite-->>DB: Ranked results (lowest distance = most similar)
    DB-->>Server: {"results": [...]}
    Server-->>Client: MCP CallToolResult
```

### Threading Flow

```mermaid
sequenceDiagram
    participant Client as AI Assistant
    participant Server as MCP Server
    participant DB as database.py
    participant SQLite as SQLite

    Client->>Server: search_emails(query="meeting notes")
    Server->>DB: search_emails()
    DB->>SQLite: FTS5 MATCH + metadata filters
    SQLite-->>DB: Results with thread_id
    DB-->>Server: EmailSearchResult[]
    Server-->>Client: Results with thread_id

    Client->>Server: get_conversation(thread_id="thread-abc123")
    Server->>DB: get_conversation()
    DB->>SQLite: SELECT WHERE thread_id = ?
    Note over DB: Fallback: subject_thread_key if no thread_id match
    SQLite-->>DB: All emails in thread
    DB-->>Server: ConversationThread
    Server-->>Client: Full conversation
```

---

## Database Schema

### Core Tables

```mermaid
erDiagram
    emails ||--o{ email_vectors : "1:1"
    emails ||--o{ emails_fts : "trigger sync"
    emails ||--o{ email_category_fts : "trigger sync"
    emails ||--o{ attachment_hashes : "references"
    project_registry ||--o{ emails : "tagged"

    emails {
        TEXT id PK "UUIDv4"
        TEXT message_id UK "RFC 822 Message-ID"
        TEXT thread_id "From References chain"
        TEXT subject_thread_key "Normalized subject"
        TEXT timestamp "ISO 8601"
        TEXT from_address "Sender email"
        TEXT from_name "Display name"
        TEXT to_addresses "JSON array"
        TEXT cc_addresses "JSON array"
        TEXT subject "Raw subject"
        TEXT body_markdown "HTML to Markdown"
        TEXT body_plain "Plain text fallback"
        TEXT x_mailer "Mail client"
        INTEGER has_attachments "0 or 1"
        TEXT attachments "JSON array"
        TEXT folder "Maildir folder"
        BLOB raw_eml "Zstd compressed"
        BLOB embedding "float32[384]"
        TEXT sender "Canonical sender"
        TEXT recipients "All recipients JSON"
        TEXT body_text "Cleaned content"
        TEXT category_tags "JSON array"
        TEXT project_tags "JSON array"
        INTEGER is_outbound "0 or 1"
        TEXT parent_id "Parent for salvaged replies"
        TEXT source "original or quoted_reply"
        TEXT content_hash "SHA-256 normalized body"
    }

    email_vectors {
        TEXT email_id "FK to emails.id"
        FLOAT embedding "384 dimensions"
    }

    attachment_hashes {
        TEXT sha256 PK
        TEXT first_email_id
        TEXT path
        TEXT filename
        TEXT mime_type
        INTEGER size_bytes
        TEXT created_at
    }

    project_registry {
        TEXT name PK
        TEXT aliases
        TEXT summary
        TEXT created_at
    }
```

---

## Troubleshooting

### MCP Connection Timeout

**Symptom:** "Operation timed out after 30000ms"

**Solutions:**
1. Use `run-mcp-server.py` (not `-m mcp_server.server`) - it handles the JSON-RPC 2.0 handshake
2. Check `/tmp/mcp_start.log` for server logs
3. Test handshake: `echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python3 run-mcp-server.py`

### Schema Validation Failure

**Symptom:** Server exits with "Missing required v2 columns"

**Solution:** Run the migration:
```bash
python3 migrate_v2.py
```

The server validates the schema on startup and will fail fast with a clear error message if v2 columns are missing.

### sqlite-vec Issues

**Symptom:** `ModuleNotFoundError: No module named 'sqlite_vec'`

```bash
conda install -c conda-forge sqlite-vec
# or
pip install sqlite-vec
```

### Vector Embeddings Not Working

**Check:**
```bash
sqlite3 db/emails.db "SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL;"
```
If 0, re-run ingestion to generate embeddings.

---

## Security

- **Parameterized Queries**: All database operations protected against SQL injection
- **Path Sanitization**: Attachment filenames sanitized to prevent traversal attacks
- **Strict Input Validation**: Pydantic models validate UUIDs, email addresses, and thread IDs
- **Schema Validation**: Server fails fast on startup if database schema is incompatible

---

## License

MIT
