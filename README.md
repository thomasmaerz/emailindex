# Email Intelligence System (emailindex)

**Email Intelligence System** is a powerful indexing and search engine for 12+ years of Maildir archives. It combines traditional full-text search with modern semantic vector search, exposed via the Model Context Protocol (MCP).

---

## Features

- **Hybrid Search**: Seamlessly combines SQLite FTS5 (BM25) with semantic vector similarity (bge-small-en-v1.5).
- **Intelligent Threading**: Reconstructs conversation threads using RFC 822 References/In-Reply-To chains with a fallback to subject-based grouping.
- **Deduplicated Attachments**: SHA-256 based deduplication ensures that multiple copies of the same file across different emails only occupy space once.
- **Resumable Ingestion**: A robust batch-based ingestion pipeline with checkpointing and error recovery.
- **Model Context Protocol (MCP)**: Exposes search and retrieval tools directly to AI assistants like Claude Desktop or OpenCode.
- **Scalable Storage**: Uses SQLite with the `sqlite-vec` extension for high-performance, single-file vector search.

---

## Tech Stack

- **Python 3.12+**
- **SQLite** (with `fts5` and `sqlite-vec` extensions)
- **Sentence-Transformers** (`BAAI/bge-small-en-v1.5`)
- **Pydantic v2** for robust data validation
- **Zstandard** for email body compression
- **BeautifulSoup4 & Markdownify** for HTML-to-Markdown conversion

---

## Getting Started

### 1. Prerequisites

Ensure you have Python 3.12+ and the following dependencies:
- `sqlite-vec` extension (native)
- `numpy`, `sentence-transformers`, `pydantic`, `zstandard`, `beautifulsoup4`, `markdownify`

### 2. Ingestion

Point the ingestion script at your Maildir directory:

```bash
python3 ingest.py /path/to/your/maildir
```

This will:
1. Parse all `.eml` files.
2. Generate 384-dimensional semantic embeddings.
3. Compress and store the raw email content.
4. Deduplicate and save attachments to `attachments/`.
5. Update the FTS index and vector database.

### 3. MCP Server

Start the MCP server in stdio mode:

```bash
python3 -m mcp_server.server --stdio
```

You can then add it to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "emailindex": {
      "command": "python3",
      "args": ["-m", "mcp_server.server", "--stdio"],
      "env": {
        "PYTHONPATH": "/path/to/emailindex"
      }
    }
  }
}
```

---

## MCP Tools

- **`search_emails`**: Search using hybrid full-text, vector similarity, or metadata filters (date, sender, recipient).
- **`get_email`**: Retrieve the complete record for a single email (including body and attachment list).
- **`get_conversation`**: Fetch all emails in a conversation thread, sorted chronologically.
- **`find_recipient_emails`**: Quickly find all emails involving a specific address.

---

## Architecture

The system is designed with a clear separation of concerns:
- **`ingest.py`**: The ingestion pipeline, handling parsing, deduplication, and storage.
- **`mcp_server/database.py`**: The persistence layer, managing SQLite connections and complex search queries.
- **`mcp_server/server.py`**: The MCP transport layer, exposing tools via JSON-RPC.
- **`mcp_server/models.py`**: Pydantic models ensuring data integrity across the system.

---

## Security

- **Parameterized Queries**: All database operations are protected against SQL injection.
- **Path Sanitization**: Attachment filenames and paths are sanitized to prevent traversal attacks.
- **Strict Validation**: All external inputs (UUIDs, thread IDs, email addresses) are validated using strict Pydantic schemas.
- **Masking**: Raw binary data is masked in API responses to prevent leakage.

---

## License

MIT
