import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import get_contact_profile
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
            sender TEXT, recipients TEXT, body_text TEXT, body_main_text TEXT,
            category_tags TEXT, project_tags TEXT, is_outbound INTEGER,
            embedding BLOB
        )
    """)
    cursor.execute("CREATE VIRTUAL TABLE emails_fts USING fts5(subject, body_markdown, content=emails, content_rowid=rowid)")

    cursor.execute("""
        INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
            to_addresses, cc_addresses, subject, body_markdown, body_text, body_main_text, folder,
            sender, recipients, is_outbound, subject_thread_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "550e8400-0001-0000-0000-000000000001", "<1@example.com>",
        "2015-03-01T10:00:00Z", "john@old.com", "John Doe",
        '["me@example.com"]', '[]', "First contact", "Hello from John",
        "Hello from John", "Hello from John", "INBOX", "john@old.com", '["me@example.com"]', 0, "first contact"
    ))
    cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", ("First contact", "Hello from John"))

    cursor.execute("""
        INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
            to_addresses, cc_addresses, subject, body_markdown, body_text, body_main_text, folder,
            sender, recipients, is_outbound, subject_thread_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "550e8400-0002-0000-0000-000000000002", "<2@example.com>",
        "2020-07-15T14:00:00Z", "john@new.com", "John Doe",
        '["me@example.com"]', '["carol@example.com"]', "Follow up", "Following up",
        "Following up", "Following up", "INBOX", "john@new.com", '["me@example.com", "carol@example.com"]', 0, "follow up"
    ))
    cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", ("Follow up", "Following up"))

    conn.commit()
    conn.close()
    return path

def test_contact_profile_by_name():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_contact_profile(name="John Doe")
        assert result is not None
        contact = result["contact"]
        assert contact["display_name"] == "John Doe"
        assert contact["total_sent_to_you"] == 2
        assert "first_interaction" in contact
        assert "last_interaction" in contact
        assert "sample_emails" in result
        assert len(result["sample_emails"]) <= 10
        print("✓ test_contact_profile_by_name passed")
    finally:
        os.unlink(path)

def test_contact_profile_by_email():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_contact_profile(email_address="john@old.com")
        assert result is not None
        assert result["contact"]["total_sent_to_you"] >= 1
        print("✓ test_contact_profile_by_email passed")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_contact_profile_by_name()
    test_contact_profile_by_email()
    print("\n✅ All contact profile tests passed!")
