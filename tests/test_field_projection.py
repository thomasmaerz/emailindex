import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import query_email_database
from mcp_server.config import Config

def create_test_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE emails (
            id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
            thread_id TEXT, subject_thread_key TEXT, timestamp TEXT NOT NULL,
            from_address TEXT NOT NULL, from_name TEXT, to_addresses TEXT NOT NULL,
            cc_addresses TEXT, subject TEXT NOT NULL, body_markdown TEXT NOT NULL,
            body_plain TEXT, x_mailer TEXT, has_attachments INTEGER NOT NULL DEFAULT 0,
            attachments TEXT, folder TEXT NOT NULL, raw_eml BLOB,
            source TEXT DEFAULT 'original', parent_id TEXT, content_hash TEXT,
            sender TEXT, recipients TEXT, body_text TEXT,
            category_tags TEXT, project_tags TEXT, is_outbound INTEGER,
            embedding BLOB
        )
    """)
    cursor.execute("CREATE VIRTUAL TABLE emails_fts USING fts5(subject, body_markdown, content=emails, content_rowid=rowid)")
    cursor.execute("""
        INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
            to_addresses, cc_addresses, subject, body_markdown, body_text, folder,
            sender, recipients, category_tags, project_tags, is_outbound, subject_thread_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "550e8400-e29b-41d4-a716-446655440000", "<test@example.com>",
        "2024-01-15T10:00:00Z", "alice@example.com", "Alice Smith",
        '["bob@example.com"]', '["carol@example.com"]', "Project Update",
        "# Full body\n\nThis is a long body with lots of text about the project update.",
        "Cleaned body text", "INBOX", "alice@example.com", '["bob@example.com", "carol@example.com"]',
        '["work"]', '["ProjectAlpha"]', 0, "project update"
    ))
    cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", ("Project Update", "Full body project update"))
    conn.commit()
    conn.close()
    return path

def test_default_fields_minimal():
    """Default response should not include body columns."""
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database()
        assert len(result["results"]) == 1
        r = result["results"][0]
        assert "body_text" not in r, f"body_text should not be in default fields"
        assert "body_markdown" not in r
        assert "body_plain" not in r
        assert "id" in r
        assert "timestamp" in r
        print("✓ test_default_fields_minimal passed")
    finally:
        os.unlink(path)

def test_custom_fields_projection():
    """Requesting specific fields returns only those fields."""
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(fields=["id", "timestamp", "from_address", "subject"])
        r = result["results"][0]
        assert set(r.keys()) == {"id", "timestamp", "from_address", "subject"}
        print("✓ test_custom_fields_projection passed")
    finally:
        os.unlink(path)

def test_snippet_only_with_fts():
    """snippet_only should return FTS5 snippet, not full body."""
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="project", snippet_only=True)
        r = result["results"][0]
        assert "snippet" in r, "snippet should be present"
        assert "body_text" not in r, "body_text should not be present with snippet_only"
        assert len(r["snippet"]) < 500, f"Snippet too long: {len(r['snippet'])}"
        print("✓ test_snippet_only_with_fts passed")
    finally:
        os.unlink(path)

def test_excluded_fields_never_returned():
    """raw_eml and embedding should never appear."""
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(fields=["id", "raw_eml", "embedding"])
        r = result["results"][0]
        assert "raw_eml" not in r
        assert "embedding" not in r
        print("✓ test_excluded_fields_never_returned passed")
    finally:
        os.unlink(path)

def test_recipients_in_default():
    """When explicitly requested, only recipients should appear (not to_addresses/cc_addresses)."""
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(fields=["id", "recipients"])
        r = result["results"][0]
        assert "recipients" in r
        assert "to_addresses" not in r
        assert "cc_addresses" not in r
        print("✓ test_recipients_in_default passed")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_default_fields_minimal()
    test_custom_fields_projection()
    test_snippet_only_with_fts()
    test_excluded_fields_never_returned()
    test_recipients_in_default()
    print("\n✅ All field projection tests passed!")
