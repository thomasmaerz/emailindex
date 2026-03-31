# Email Intelligence System (emailindex)

**Email Intelligence System** is a powerful indexing and search engine for 12+ years of Maildir archives. It combines traditional full-text search with modern semantic vector search, exposed via the Model Context Protocol (MCP).

---

## Features

- **Hybrid Search**: Seamlessly combines SQLite FTS5 (BM25) with semantic vector similarity.
- **Cosine Similarity Ranking**: Vector searches return precise similarity scores using `sqlite-vec`'s `vec_distance_cosine()` for high-performance relevance ranking.
- **Intelligent Threading**: Reconstructs conversation threads using RFC 822 References/In-Reply-To chains with a robust **subject-based fallback** for emails missing standard headers.
- **Deduplicated Attachments**: SHA-256 based deduplication ensures that multiple copies of the same file across different emails only occupy space once.
- **Resumable Ingestion**: A robust batch-based ingestion pipeline with checkpointing and error recovery.
- **Concurrency Support**: Thread-local database connections allow multiple MCP requests to be handled safely without SQLite lock contention.
- **Model Context Protocol (MCP)**: Exposes search and retrieval tools directly to AI assistants like Claude Desktop or OpenCode.
- **Scalable Storage**: Uses SQLite with the `sqlite-vec` extension for high-performance, single-file vector search.

---

## Tech Stack

- **Python 3.12+**
- **Conda** (Recommended for dependency management)
- **SQLite** (with `fts5` and `sqlite-vec` extensions)
- **Sentence-Transformers** (`BAAI/bge-small-en-v1.5`)
- **Pydantic v2** for robust data validation
- **Zstandard** for email body compression
- **BeautifulSoup4 & Markdownify** for HTML-to-Markdown conversion

---

## Getting Started

### 1. Installation (Conda Recommended)

The most reliable way to manage the native dependencies (like `sqlite-vec`) is using a Conda environment:

```bash
# Create the environment
conda create -n emailindex python=3.12 -y

# Activate it
conda activate emailindex

# Install dependencies
pip install -r requirements.txt
```

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

The MCP server uses a wrapper script that handles the JSON-RPC 2.0 protocol required by modern MCP clients (OpenCode, Claude Desktop):

```bash
python3 run-mcp-server.py --stdio
```

Or equivalently:

```bash
./run-mcp-server.sh
```

> **Note:** The wrapper script (`run-mcp-server.py`) is required because it handles the MCP protocol handshake (`initialize`, `initialized`) and formats all responses as proper JSON-RPC 2.0.

---

## OpenCode / Claude Desktop Integration

Add the following to your OpenCode configuration (`~/.config/opencode/opencode.json`):

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

> **Important:** Use the wrapper script path (`run-mcp-server.py`), not the module path (`-m mcp_server.server`). The wrapper handles the required JSON-RPC 2.0 protocol handshake.

For Claude Desktop, use:

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

- **`search_emails`**: Search using hybrid full-text, vector similarity (with scores), or metadata filters (date, sender, recipient).
- **`get_email`**: Retrieve the complete record for a single email (including body and attachment list).
- **`get_conversation`**: Fetch all emails in a conversation thread. Supports standard References chains and subject-based fallback.
- **`find_recipient_emails`**: Quickly find all emails involving a specific address.

---

## Architecture

- **`run-mcp-server.py`**: Wrapper script that handles JSON-RPC 2.0 protocol (initialize handshake, response formatting). This is the entry point for MCP clients.
- **`run-mcp-server.sh`**: Shell script alternative for starting the MCP server.
- **`ingest.py`**: The ingestion pipeline, handling parsing, deduplication, and storage.
- **`mcp_server/server.py`**: Core MCP server logic - tool definitions and request handling.
- **`mcp_server/database.py`**: The persistence layer, managing **thread-local SQLite connections** and complex search queries (FTS5 + vector similarity).
- **`mcp_server/models.py`**: Pydantic models ensuring strict data integrity (UUID, Email, and ThreadID validation).
- **`mcp_server/config.py`**: Configuration constants (paths, model settings, timeouts).

---

## Troubleshooting

### MCP Connection Timeout

**Symptom:** OpenCode shows "Operation timed out after 30000ms" for emailindex.

**Causes & Solutions:**

1. **Missing JSON-RPC 2.0 wrapper**
   - Ensure you're using `run-mcp-server.py`, not `-m mcp_server.server`
   - The wrapper handles the required `initialize` handshake

2. **Wrong response format**
   - Responses must include `jsonrpc`, `id`, and `result` fields
   - Check server logs at `/tmp/mcp_start.log` for debugging

3. **Test the handshake manually:**
   ```bash
   echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' | python3 run-mcp-server.py
   ```
   Should return: `{"jsonrpc": "2.0", "id": 1, "result": {...}}`

### sqlite-vec Installation Issues

**Symptom:** `ModuleNotFoundError: No module named 'sqlite_vec'`

**Solutions:**

1. **Using conda (recommended):**
   ```bash
   conda install -c conda-forge sqlite-vec
   ```

2. **Using pip:**
   ```bash
   pip install sqlite-vec
   ```

3. **Verify installation:**
   ```bash
   python3 -c "import sqlite_vec; print('sqlite-vec loaded')"
   ```

### Import Errors

**Symptom:** `ModuleNotFoundError: No module named 'mcp_server'`

**Solution:** The wrapper script sets `PYTHONPATH` automatically. If running manually:
```bash
export PYTHONPATH=/path/to/emailindex
python3 -m mcp_server.server --stdio
```

### Vector Search is Slow

**Symptom:** Similar email search takes >10 seconds.

**Cause:** Using Python-based vector computation instead of sqlite-vec.

**Solution:** Ensure sqlite-vec is loaded and using `vec_distance_cosine()`:
```python
# Fast - uses SQLite/C
cursor.execute("""
    SELECT id, vec_distance_cosine(embedding, ?) as score
    FROM emails WHERE embedding IS NOT NULL
    ORDER BY score DESC LIMIT ?
""", (query_embedding, limit))
```

### Database Lock Errors

**Symptom:** `database is locked` errors during ingestion.

**Solution:** The server uses thread-local connections. For ingestion, ensure no MCP clients are connected, or increase the timeout in `config.py`:
```python
DB_TIMEOUT = 60.0  # Increase from 30.0
```

### Vector Embeddings Not Working

**Symptom:** Vector search returns no results or errors.

**Check:**
1. Embeddings exist: `sqlite3 db/emails.db "SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL;"`
2. If 0, re-run ingestion to generate embeddings
3. If errors, check that `sentence-transformers` model downloads correctly

---

## Security

- **Parameterized Queries**: All database operations are protected against SQL injection.
- **Path Sanitization**: Attachment filenames and paths are sanitized to prevent traversal attacks.
- **Strict Input Validation**: All IDs and email addresses are validated using strict regex and type checks.
- **Masking**: Raw binary data is masked in API responses to prevent leakage.

---

## License

MIT
