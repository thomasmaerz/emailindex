import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import query_email_database
from mcp_server.config import Config

def create_test_db_with_many_emails():
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

    for i in range(15):
        uid = f"550e8400-0000-0000-0000-{i:012d}"
        ts = f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00Z"
        cursor.execute("""
            INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
                to_addresses, subject, body_markdown, body_text, folder, sender, recipients,
                subject_thread_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, f"<{i}@example.com>", ts, f"user{i}@example.com", f"User {i}",
              '["me@example.com"]', f"Test email {i}", f"Body {i}", f"Body {i}",
              "INBOX", f"user{i}@example.com", '["me@example.com"]', f"test email {i}"))
        cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", (f"Test {i}", f"Body {i}"))

    conn.commit()
    conn.close()
    return path

def test_first_page_has_next_cursor():
    path = create_test_db_with_many_emails()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(limit=5)
        assert "next_cursor" in result, f"next_cursor missing: {result.keys()}"
        assert "has_more" in result
        assert result["has_more"] is True
        assert len(result["results"]) == 5
        print("✓ test_first_page_has_next_cursor passed")
    finally:
        os.unlink(path)

def test_cursor_pagination_continues():
    path = create_test_db_with_many_emails()
    try:
        Config.DB_PATH = Path(path)
        page1 = query_email_database(limit=5)
        assert page1["has_more"] is True

        page2 = query_email_database(limit=5, cursor=page1["next_cursor"])
        assert len(page2["results"]) == 5
        page1_ids = {r["id"] for r in page1["results"]}
        page2_ids = {r["id"] for r in page2["results"]}
        assert len(page1_ids & page2_ids) == 0, "Pages should not overlap"
        print("✓ test_cursor_pagination_continues passed")
    finally:
        os.unlink(path)

def test_last_page_has_no_more():
    path = create_test_db_with_many_emails()
    try:
        Config.DB_PATH = Path(path)
        page1 = query_email_database(limit=5)
        page2 = query_email_database(limit=5, cursor=page1["next_cursor"])
        page3 = query_email_database(limit=5, cursor=page2["next_cursor"])
        page4 = query_email_database(limit=5, cursor=page3["next_cursor"])
        assert page4["has_more"] is False
        assert page4["next_cursor"] is None
        print("✓ test_last_page_has_no_more passed")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_first_page_has_next_cursor()
    test_cursor_pagination_continues()
    test_last_page_has_no_more()
    print("\n✅ All cursor pagination tests passed!")
