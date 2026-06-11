import sys, tempfile, sqlite3, os
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.database import query_email_database, _get_embedding_model, initialize_embedding_model_async
from mcp_server.config import Config


def create_fake_db_connection():
    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

    fake_cursor = FakeCursor()
    fake_conn = FakeConnection(fake_cursor)
    return fake_conn, fake_cursor

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
        ("001", "2013-01-01T10:00:00Z", "john@old.com", "John Doe", "john@old.com", '["me@example.com"]', "Old mention of John", "John Doe discussed the project.", "[]", "[]"),
        ("002", "2016-06-15T14:00:00Z", "john@mid.com", "John Doe", "john@mid.com", '["me@example.com"]', "Mid-era John email", "Meeting with John Doe went well.", "[]", "[]"),
        ("003", "2020-03-20T09:00:00Z", "john@new.com", "J. Doe", "john@new.com", '["me@example.com"]', "Recent John", "John Doe approved the budget.", "[]", "[]"),
        ("004", "2022-12-01T16:00:00Z", "alice@corp.com", "Alice Smith", "alice@corp.com", '["me@example.com"]', "Alice email", "No John here, just Alice.", "[]", "[]"),
        ("005", "2021-04-10T11:30:00Z", "finance@corp.com", "Finance Bot", "finance@corp.com", '["me@example.com"]', "Budget update", "Project Alpha finance update.", '["finance"]', '["alpha"]'),
    ]
    for eid, ts, sender, name, sender2, recip, subj, body, category_tags, project_tags in emails:
        uid = f"550e8400-0000-0000-0000-000000000{eid}"
        cursor.execute("""
            INSERT INTO emails (id, message_id, timestamp, from_address, from_name,
                to_addresses, subject, body_markdown, body_text, folder, sender, recipients,
                category_tags, project_tags, is_outbound, subject_thread_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, f"<{eid}@example.com>", ts, sender, name, recip, subj, body, body, "INBOX", sender2, recip, category_tags, project_tags, 0, subj.lower()))
        cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (last_insert_rowid(), ?, ?)", (subj, body))

    cursor.execute("""
        CREATE TABLE email_vectors (
            email_id TEXT PRIMARY KEY,
            embedding BLOB
        )
    """)
    for eid in ["001", "002", "003", "004", "005"]:
        uid = f"550e8400-0000-0000-0000-000000000{eid}"
        cursor.execute("INSERT INTO email_vectors(email_id, embedding) VALUES (?, ?)", (uid, b"fake-vector"))

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


def test_tag_filters_bind_all_placeholders():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(category_filter="finance,work", project_filter="alpha")
        assert "results" in result, f"Expected results key, got {result.keys()}"
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_fts_snippet_query_uses_join_alias():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="John Doe", snippet_only=True, limit=2)
        assert len(result["results"]) >= 1, f"Expected snippet results, got {result}"
        assert "snippet" in result["results"][0], f"Expected snippet field, got {result['results'][0].keys()}"
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_semantic_query_returns_timeout_error_without_loading_model():
    class FakeCursor:
        def execute(self, sql, params):
            raise AssertionError("Query should not execute when embedding encoding fails")

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    with patch("mcp_server.database.get_connection", return_value=FakeConnection()), \
         patch("mcp_server.database.close_connection"), \
         patch("mcp_server.database._encode_text_to_embedding", side_effect=TimeoutError("model init timed out")):
        result = query_email_database(semantic_query="project update")
    assert result["results"] == [], f"Expected empty results on semantic timeout, got {result}"
    assert "Failed to encode semantic query" in result["error"], f"Expected semantic error, got {result}"


def test_semantic_query_with_filters_applies_shared_where_clause():
    fake_conn, fake_cursor = create_fake_db_connection()

    with patch("mcp_server.database._encode_text_to_embedding", return_value=b"fake-embedding"), \
         patch("mcp_server.database.get_connection", return_value=fake_conn), \
         patch("mcp_server.database.close_connection"):
        result = query_email_database(
            semantic_query="project update",
            category_filter="finance",
            project_filter="alpha",
            limit=10,
        )

    assert result["results"] == [], f"Expected empty semantic result set from fake cursor, got {result}"
    assert len(fake_cursor.calls) == 1, f"Expected exactly one semantic query execution, got {fake_cursor.calls}"
    sql, params = fake_cursor.calls[0]
    assert "category_tags" in sql, f'Expected "category_tags" in SQL, got {sql}'
    assert "project_tags" in sql, f'Expected "project_tags" in SQL, got {sql}'
    # Tags appear twice because the shared WHERE clause binds the same tag list
    # once for category_tags and once again for project_tags.
    assert params == [b"fake-embedding", "%finance%", "%alpha%", "%finance%", "%alpha%", 10], (
        f"Expected semantic params to include shared filter placeholders, got {params}"
    )


def test_semantic_query_with_date_filter_applies_shared_where_clause():
    fake_conn, fake_cursor = create_fake_db_connection()

    with patch("mcp_server.database._encode_text_to_embedding", return_value=b"fake-embedding"), \
         patch("mcp_server.database.get_connection", return_value=fake_conn), \
         patch("mcp_server.database.close_connection"):
        result = query_email_database(
            semantic_query="meeting agenda",
            date_from="2030-01-01",
            limit=10,
        )

    assert result["results"] == [], f"Expected empty semantic result set from fake cursor, got {result}"
    assert len(fake_cursor.calls) == 1, f"Expected exactly one semantic query execution, got {fake_cursor.calls}"
    sql, params = fake_cursor.calls[0]
    assert "e.timestamp >= ?" in sql, f"Expected date filter in semantic SQL, got {sql}"
    assert params == [b"fake-embedding", "2030-01-01", 10], (
        f"Expected semantic params to include date filter placeholder, got {params}"
    )


def test_semantic_query_with_is_outbound_filter_applies_shared_where_clause():
    fake_conn, fake_cursor = create_fake_db_connection()

    with patch("mcp_server.database._encode_text_to_embedding", return_value=b"fake-embedding"), \
         patch("mcp_server.database.get_connection", return_value=fake_conn), \
         patch("mcp_server.database.close_connection"):
        result = query_email_database(
            semantic_query="project update",
            is_outbound=True,
            limit=5,
        )

    assert result["results"] == [], f"Expected empty semantic result set from fake cursor, got {result}"
    assert len(fake_cursor.calls) == 1, f"Expected exactly one semantic query execution, got {fake_cursor.calls}"
    sql, params = fake_cursor.calls[0]
    assert "e.is_outbound = ?" in sql, f"Expected outbound filter in semantic SQL, got {sql}"
    assert params == [b"fake-embedding", 1, 5], (
        f"Expected semantic params to include outbound filter placeholder, got {params}"
    )


def test_semantic_query_with_is_outbound_false_filter_applies_shared_where_clause():
    fake_conn, fake_cursor = create_fake_db_connection()

    with patch("mcp_server.database._encode_text_to_embedding", return_value=b"fake-embedding"), \
         patch("mcp_server.database.get_connection", return_value=fake_conn), \
         patch("mcp_server.database.close_connection"):
        result = query_email_database(
            semantic_query="project update",
            is_outbound=False,
            limit=5,
        )

    assert result["results"] == [], f"Expected empty semantic result set from fake cursor, got {result}"
    assert len(fake_cursor.calls) == 1, f"Expected exactly one semantic query execution, got {fake_cursor.calls}"
    sql, params = fake_cursor.calls[0]
    assert "e.is_outbound = ?" in sql, f"Expected outbound filter in semantic SQL, got {sql}"
    assert params == [b"fake-embedding", 0, 5], (
        f"Expected semantic params to normalize outbound false to 0, got {params}"
    )


def test_whitespace_keywords_do_not_enable_fts_join():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(exact_keywords="   ", sort_by="timestamp", limit=2)
        assert len(result["results"]) == 2, f"Expected normal non-FTS query results, got {result}"
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_limit_is_clamped_to_documented_maximum():
    path = create_test_db()
    original_db = Config.DB_PATH
    try:
        Config.DB_PATH = Path(path)
        result = query_email_database(limit=999)
        assert len(result["results"]) == 5, f"Expected existing rows only after clamp, got {len(result['results'])}"
        assert result["has_more"] is False, f"Expected has_more false for clamped limit, got {result}"
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_get_embedding_model_returns_initializing_error_without_blocking():
    with patch("mcp_server.database.threading.Thread") as thread_mock, \
         patch("mcp_server.database._embedding_model", None), \
         patch("mcp_server.database._embedding_model_load_error", None), \
         patch("mcp_server.database._embedding_model_loading", False):
        try:
            _get_embedding_model()
            assert False, "Expected initialization-in-progress error"
        except RuntimeError as exc:
            assert "still initializing" in str(exc), f"Unexpected error: {exc}"
        thread_mock.assert_called_once()


def test_initialize_embedding_model_async_retries_after_previous_failure():
    with patch("mcp_server.database.threading.Thread") as thread_mock, \
         patch("mcp_server.database._embedding_model", None), \
         patch("mcp_server.database._embedding_model_loading", False), \
         patch("mcp_server.database._embedding_model_load_error", RuntimeError("temporary failure")):
        initialize_embedding_model_async()

        thread_mock.assert_called_once()

if __name__ == "__main__":
    test_count_only_returns_count()
    test_sort_by_timestamp_asc()
    test_sort_by_timestamp_desc()
    test_from_name_filter()
    test_relevance_score_in_fts_results()
    test_tag_filters_bind_all_placeholders()
    test_fts_snippet_query_uses_join_alias()
    test_semantic_query_returns_timeout_error_without_loading_model()
    test_semantic_query_with_filters_applies_shared_where_clause()
    test_semantic_query_with_date_filter_applies_shared_where_clause()
    test_semantic_query_with_is_outbound_filter_applies_shared_where_clause()
    test_semantic_query_with_is_outbound_false_filter_applies_shared_where_clause()
    test_whitespace_keywords_do_not_enable_fts_join()
    test_limit_is_clamped_to_documented_maximum()
    test_get_embedding_model_returns_initializing_error_without_blocking()
    test_initialize_embedding_model_async_retries_after_previous_failure()
    print("\n✅ All query extension tests passed!")
