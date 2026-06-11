import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import get_email, get_conversation, close_connection
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
    cursor.execute("""
        INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
            to_addresses, subject, body_markdown, body_text, folder, sender, recipients, subject_thread_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "550e8400-e29b-41d4-a716-446655440000", "<test@example.com>",
        "2024-01-15T10:00:00Z", "alice@example.com", "Alice",
        '["bob@example.com"]', "Test", "Test body", "Test body",
        "INBOX", "alice@example.com", '["bob@example.com"]', "test"
    ))
    conn.commit()
    conn.close()
    return path


def update_test_blobs(path: str):
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE emails SET raw_eml = X'DEADBEEF', embedding = X'01020304' WHERE id = '550e8400-e29b-41d4-a716-446655440000'"
    )
    conn.commit()
    conn.close()

def test_get_email_excludes_raw_eml():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        update_test_blobs(path)

        Config.DB_PATH = Path(path)

        result = get_email("550e8400-e29b-41d4-a716-446655440000")
        assert result is not None
        assert result.raw_eml is None, f"raw_eml should be None, got {type(result.raw_eml)}"
        print("✓ test_get_email_excludes_raw_eml passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)

def test_get_email_excludes_embedding():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        Config.DB_PATH = Path(path)

        result = get_email("550e8400-e29b-41d4-a716-446655440000")
        assert result is not None
        dumped = result.model_dump(mode='json')
        assert "embedding" not in dumped, f"embedding should not be in response, got keys: {dumped.keys()}"
        print("✓ test_get_email_excludes_embedding passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)

def test_get_conversation_fallback_excludes_blob_fields():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        update_test_blobs(path)

        Config.DB_PATH = Path(path)

        result = get_conversation("thread-test")
        assert result is not None
        dumped = result.model_dump(mode='json')

        assert dumped["emails"][0]["raw_eml"] is None, (
            f"raw_eml should be None in fallback conversation path, got {type(dumped['emails'][0]['raw_eml'])}"
        )
        assert "embedding" not in dumped["emails"][0], (
            f"embedding should not be in fallback conversation response, got keys: {dumped['emails'][0].keys()}"
        )
    finally:
        close_connection()
        Config.DB_PATH = original_db
        os.unlink(path)


def test_get_conversation_primary_thread_path_excludes_blob_fields():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        update_test_blobs(path)

        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE emails SET thread_id = 'thread-real' WHERE id = '550e8400-e29b-41d4-a716-446655440000'"
        )
        conn.commit()
        conn.close()

        Config.DB_PATH = Path(path)

        result = get_conversation("thread-real")
        assert result is not None
        dumped = result.model_dump(mode='json')

        assert dumped["emails"][0]["raw_eml"] is None, (
            f"raw_eml should be None in primary thread path, got {type(dumped['emails'][0]['raw_eml'])}"
        )
        assert "embedding" not in dumped["emails"][0], (
            f"embedding should not be in primary thread response, got keys: {dumped['emails'][0].keys()}"
        )
    finally:
        close_connection()
        Config.DB_PATH = original_db
        os.unlink(path)

if __name__ == "__main__":
    test_get_email_excludes_raw_eml()
    test_get_email_excludes_embedding()
    test_get_conversation_fallback_excludes_blob_fields()
    test_get_conversation_primary_thread_path_excludes_blob_fields()
    print("\n✅ All blob exclusion tests passed!")
