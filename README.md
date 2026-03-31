# Email Intelligence System (emailindex)

**Email Intelligence System** is a powerful indexing and search engine for 12+ years of Maildir archives. It combines traditional full-text search with modern semantic vector search, exposed via the Model Context Protocol (MCP).

---

## Features

- **Hybrid Search**: Seamlessly combines SQLite FTS5 (BM25) with semantic vector similarity.
- **Cosine Similarity Ranking**: Vector searches now return precise similarity scores calculated using `numpy` for better relevance ranking.
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

Start the MCP server in stdio mode:

```bash
python3 -m mcp_server.server --stdio
```

---

## OpenCode / Claude Desktop Integration

Add the following to your `opencode.json` or `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "emailindex": {
      "type": "local",
      "command": ["/path/to/miniconda3/envs/emailindex/bin/python3", "-m", "mcp_server.server", "--stdio"],
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

- **`ingest.py`**: The ingestion pipeline, handling parsing, deduplication, and storage.
- **`mcp_server/database.py`**: The persistence layer, managing **thread-local SQLite connections** and complex search queries.
- **`mcp_server/server.py`**: The MCP transport layer, exposing tools via JSON-RPC.
- **`mcp_server/models.py`**: Pydantic models ensuring strict data integrity (UUID, Email, and ThreadID validation).

---

## Security

- **Parameterized Queries**: All database operations are protected against SQL injection.
- **Path Sanitization**: Attachment filenames and paths are sanitized to prevent traversal attacks.
- **Strict Input Validation**: All IDs and email addresses are validated using strict regex and type checks.
- **Masking**: Raw binary data is masked in API responses to prevent leakage.

---

## License

MIT
