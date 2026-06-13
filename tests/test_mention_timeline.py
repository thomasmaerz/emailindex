import sys, tempfile, sqlite3, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import get_mention_timeline
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

    for year in range(2015, 2023):
        for i in range(year - 2014):
            uid = f"550e8400-{year:04d}-{i:04d}-0000-000000000000"
            ts = f"{year}-06-15T10:00:00Z"
            body_markdown = f"John Doe mentioned in {year} ftsonlytoken{year}{i}"
            cursor.execute("""
                INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
                    to_addresses, subject, body_markdown, body_text, body_main_text, folder, sender, recipients,
                    subject_thread_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (uid, f"<{year}-{i}@example.com>", ts, "alice@example.com", "Alice",
                  '["me@example.com"]', f"Mention {year}-{i}", body_markdown,
                  f"John Doe {year}", f"John Doe {year}", "INBOX", "alice@example.com", '["me@example.com"]', f"mention {year}"))
            cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", (f"Mention {year}", body_markdown))

    conn.commit()
    conn.close()
    return path

def test_timeline_yearly():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_mention_timeline(keyword="John Doe", granularity="year")
        assert result["keyword"] == "John Doe"
        assert result["total_matches"] > 0
        assert "timeline" in result
        assert "first_occurrence" in result
        assert "last_occurrence" in result
        assert "2015" in result["timeline"]
        assert "2022" in result["timeline"]
        print("✓ test_timeline_yearly passed")
    finally:
        os.unlink(path)

def test_timeline_with_filters():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_mention_timeline(keyword="John Doe", date_from="2018-01-01", date_to="2020-12-31")
        assert result["total_matches"] > 0
        for key in result["timeline"]:
            assert 2018 <= int(key) <= 2020, f"Year {key} outside filter range"
        print("✓ test_timeline_with_filters passed")
    finally:
        os.unlink(path)


def test_timeline_uses_fts_table_name_in_match_clause():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = get_mention_timeline(keyword="ftsonlytoken20150", granularity="month")
        assert result["total_matches"] > 0, f"Expected timeline matches, got {result}"
        assert any(period.startswith("2015-") for period in result["timeline"].keys()), (
            f"Expected month-granularity timeline periods, got {result['timeline']}"
        )
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_timeline_yearly()
    test_timeline_with_filters()
    test_timeline_uses_fts_table_name_in_match_clause()
    print("\n✅ All mention timeline tests passed!")
