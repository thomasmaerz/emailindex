import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import get_thread_arc
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

    thread_id = "thread-abc123"
    for i in range(5):
        uid = f"550e8400-thread-{i:04d}"
        sender = "alice@example.com" if i % 2 == 0 else "bob@example.com"
        name = "Alice" if i % 2 == 0 else "Bob"
        cursor.execute("""
            INSERT INTO emails (id, message_id, thread_id, timestamp, from_address, from_name,
                to_addresses, subject, body_markdown, body_text, folder, sender, recipients, is_outbound,
                subject_thread_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, f"<thread-{i}@example.com>", thread_id,
              f"2024-01-{10+i:02d}T10:00:00Z", sender, name,
              '["me@example.com"]', "Thread Subject",
              f"Message {i} body content with some details about the discussion",
              f"Message {i}", "INBOX", sender, '["me@example.com"]', 0, "thread subject"))

    conn.commit()
    conn.close()
    return path

def test_thread_arc_summary():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_thread_arc("thread-abc123", mode="summary")
        assert result is not None
        assert result["thread_id"] == "thread-abc123"
        assert result["subject"] == "Thread Subject"
        assert result["message_count"] == 5
        assert len(result["participants"]) == 2
        assert len(result["messages"]) == 5
        for msg in result["messages"]:
            assert "snippet" in msg
            assert "direction" in msg
        print("✓ test_thread_arc_summary passed")
    finally:
        os.unlink(path)

def test_thread_arc_max_messages():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_thread_arc("thread-abc123", mode="summary", max_messages=2)
        assert len(result["messages"]) == 2
        print("✓ test_thread_arc_max_messages passed")
    finally:
        os.unlink(path)

def test_thread_arc_not_found():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_thread_arc("thread-nonexistent")
        assert result is None
        print("✓ test_thread_arc_not_found passed")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_thread_arc_summary()
    test_thread_arc_max_messages()
    test_thread_arc_not_found()
    print("\n✅ All thread arc tests passed!")
