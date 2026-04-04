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

    emails = [
        ("001", "2013-01-01T10:00:00Z", "john@old.com", "John Doe", "john@old.com", '["me@example.com"]', "Old mention of John", "John Doe discussed the project."),
        ("002", "2016-06-15T14:00:00Z", "john@mid.com", "John Doe", "john@mid.com", '["me@example.com"]', "Mid-era John email", "Meeting with John Doe went well."),
        ("003", "2020-03-20T09:00:00Z", "john@new.com", "J. Doe", "john@new.com", '["me@example.com"]', "Recent John", "John Doe approved the budget."),
        ("004", "2022-12-01T16:00:00Z", "alice@corp.com", "Alice Smith", "alice@corp.com", '["me@example.com"]', "Alice email", "No John here, just Alice."),
    ]
    for eid, ts, sender, name, sender2, recip, subj, body in emails:
        uid = f"550e8400-0000-0000-0000-000000000{eid}"
        cursor.execute("""
            INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
                to_addresses, subject, body_markdown, body_text, folder, sender, recipients,
                category_tags, project_tags, is_outbound, subject_thread_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, f"<{eid}@example.com>", ts, sender, name, recip, subj, body, body, "INBOX", sender2, recip, "[]", "[]", 0, subj.lower()))
        cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", (subj, body))

    conn.commit()
    conn.close()
    return path

def test_count_only_returns_count():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="John Doe", count_only=True)
        assert "count" in result, f"Expected 'count' key, got {result.keys()}"
        assert result["count"] == 3, f"Expected 3 matches, got {result['count']}"
        assert "results" not in result, "count_only should not include results"
        print("✓ test_count_only_returns_count passed")
    finally:
        os.unlink(path)

def test_sort_by_timestamp_asc():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="John Doe", sort_by="timestamp", sort_order="asc", limit=1)
        assert len(result["results"]) == 1
        assert "2013" in result["results"][0]["timestamp"], f"Expected oldest email, got {result['results'][0]['timestamp']}"
        print("✓ test_sort_by_timestamp_asc passed")
    finally:
        os.unlink(path)

def test_sort_by_timestamp_desc():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="John Doe", sort_by="timestamp", sort_order="desc", limit=1)
        assert len(result["results"]) == 1
        assert "2020" in result["results"][0]["timestamp"], f"Expected newest email, got {result['results'][0]['timestamp']}"
        print("✓ test_sort_by_timestamp_desc passed")
    finally:
        os.unlink(path)

def test_from_name_filter():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(from_name="John Doe")
        assert len(result["results"]) >= 2, f"Expected >= 2 results for 'John Doe', got {len(result['results'])}"
        for r in result["results"]:
            assert "John" in r["from_name"] or "Doe" in r["from_name"], f"from_name mismatch: {r['from_name']}"
        print("✓ test_from_name_filter passed")
    finally:
        os.unlink(path)

def test_relevance_score_in_fts_results():
    path = create_test_db()
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="John Doe")
        for r in result["results"]:
            assert "relevance_score" in r, f"relevance_score missing from result: {r.keys()}"
            assert r["relevance_score"] is not None, "relevance_score should not be null for FTS query"
            assert isinstance(r["relevance_score"], (int, float)), f"relevance_score should be numeric, got {type(r['relevance_score'])}"
        print("✓ test_relevance_score_in_fts_results passed")
    finally:
        os.unlink(path)

if __name__ == "__main__":
    test_count_only_returns_count()
    test_sort_by_timestamp_asc()
    test_sort_by_timestamp_desc()
    test_from_name_filter()
    test_relevance_score_in_fts_results()
    print("\n✅ All query extension tests passed!")
