import pytest
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock, patch

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import migrate_body_text

from migrate_body_text import markdown_contains_formatting, markdown_to_plain_text
from mcp_server.database import query_email_database
from mcp_server.config import Config
from tests.validate_body_text import count_markdown_copies
from ingest import init_database


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Heading\n\nA **bold** [link](https://example.com)", "Heading A bold link"),
        ("- one\n- two", "one two"),
        ("> quoted\n\nplain", "quoted plain"),
    ],
)
def test_markdown_to_plain_text_removes_markdown_syntax(markdown, expected):
    assert markdown_to_plain_text(markdown) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Heading", True),
        ("Please review [the doc](https://example.com)", True),
        ("Capital ID # 714 was approved.", False),
        ("Plain text only", False),
    ],
)
def test_markdown_contains_formatting_identifies_real_markdown(text, expected):
    assert markdown_contains_formatting(text) is expected


def test_markdown_to_plain_text_keeps_image_only_markdown_empty():
    assert markdown_contains_formatting("![](cid:image001.png@01D2C80A.BA8433A0)") is True
    assert markdown_to_plain_text("![](cid:image001.png@01D2C80A.BA8433A0)") == ""


def test_markdown_to_plain_text_handles_empty_target_links_and_images():
    assert markdown_contains_formatting("![inline]()") is True
    assert markdown_to_plain_text("![inline]()") == "inline"
    assert markdown_to_plain_text("[portal](/path)") == "portal"


def test_count_markdown_copies_ignores_plain_text_hash_characters():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE emails (body_markdown TEXT, body_text TEXT)")
    cur.executemany(
        "INSERT INTO emails(body_markdown, body_text) VALUES (?, ?)",
        [
            ("Capital ID # 714 was approved.", "Capital ID # 714 was approved."),
            ("# Heading\n\nBody", "# Heading\n\nBody"),
            ("Get [Outlook for iOS](https://aka.ms/o0ukef)", "Get [Outlook for iOS](https://aka.ms/o0ukef)"),
        ],
    )

    assert count_markdown_copies(conn) == 2
    conn.close()


def test_count_markdown_copies_ignores_non_transforming_angle_brackets_and_shebangs():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE emails (body_markdown TEXT, body_text TEXT)")
    cur.executemany(
        "INSERT INTO emails(body_markdown, body_text) VALUES (?, ?)",
        [
            ("On Apr 26, 2021, at 03:19 PM, David Harris <david@example.com> wrote:", "On Apr 26, 2021, at 03:19 PM, David Harris <david@example.com> wrote:"),
            ("#!/bin/bash\nset -x", "#!/bin/bash\nset -x"),
            ("[portal](https://example.com)", "[portal](https://example.com)"),
        ],
    )

    assert count_markdown_copies(conn) == 1
    conn.close()


def test_main_backfills_before_rebuilding_fts():
    order = []
    conn = Mock()
    cursor = Mock()
    conn.cursor.return_value = cursor

    def record(name, return_value=None):
        def _recorder(*args, **kwargs):
            order.append(name)
            return return_value
        return _recorder

    cursor.fetchall.return_value = [(0, "body_text"), (1, "body_main_text")]

    with patch.object(type(migrate_body_text.DB_PATH), "exists", return_value=True), \
         patch.object(migrate_body_text.sqlite3, "connect", return_value=conn), \
         patch.object(migrate_body_text, "fts_uses_body_text", return_value=False), \
         patch.object(migrate_body_text, "backfill_body_text", side_effect=record("backfill", 12)), \
         patch.object(migrate_body_text, "drop_fts_objects", side_effect=record("drop")), \
         patch.object(migrate_body_text, "create_fts_objects", side_effect=record("create")), \
         patch.object(migrate_body_text, "rebuild_fts", side_effect=record("rebuild")):
        migrate_body_text.main()

    assert order == ["backfill", "drop", "create", "rebuild"]


def test_emails_table_includes_body_main_text(tmp_path):
    db_path = tmp_path / "emails.db"

    init_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cur.fetchall()}
        assert "body_main_text" in columns
    finally:
        conn.close()


def test_body_main_text_backfills_without_nulls_when_body_text_exists():
    conn = sqlite3.connect(":memory:")
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE emails (rowid INTEGER PRIMARY KEY, body_markdown TEXT, body_plain TEXT, body_text TEXT, body_main_text TEXT)"
        )
        cur.execute(
            "INSERT INTO emails(body_markdown, body_plain, body_text, body_main_text) VALUES (?, ?, ?, ?)",
            ("# Heading", None, "Heading", None),
        )

        updated = migrate_body_text.backfill_body_text(conn, cur, batch_size=10)

        cur.execute("SELECT body_text, body_main_text FROM emails")
        body_text, body_main_text = cur.fetchone()
        assert updated == 1
        assert body_text == "Heading"
        assert body_main_text is not None
        assert body_main_text != ""
    finally:
        conn.close()


def test_projection_can_surface_body_main_text_when_requested(tmp_path):
    db_path = tmp_path / "projection.db"
    init_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO emails (
                id, message_id, thread_id, subject_thread_key, timestamp,
                from_address, from_name, to_addresses, cc_addresses,
                subject, body_markdown, body_plain, body_text, body_main_text,
                x_mailer, has_attachments, attachments, folder, raw_eml, embedding,
                source, parent_id, content_hash, sender, recipients,
                category_tags, project_tags, is_outbound
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "550e8400-e29b-41d4-a716-446655440000",
                "<projection@example.com>",
                "thread-projection",
                "projection",
                "2026-06-13T12:00:00Z",
                "captain@example.com",
                "Captain",
                '["crew@example.com"]',
                None,
                "Projection test",
                "Raw markdown",
                None,
                "Faithful body text",
                "Cleaner retrieval text",
                None,
                0,
                "[]",
                "INBOX",
                None,
                None,
                "original",
                None,
                None,
                "captain@example.com",
                '["crew@example.com"]',
                "[]",
                "[]",
                0,
            ),
        )
        cur.execute(
            "INSERT INTO emails_fts(rowid, subject, body_text) VALUES (last_insert_rowid(), ?, ?)",
            ("Projection test", "Faithful body text"),
        )
        conn.commit()

        original_db_path = Config.DB_PATH
        Config.DB_PATH = db_path
        try:
            result = query_email_database(
                exact_keywords="Faithful",
                limit=1,
                fields=["id", "body_main_text"],
            )
        finally:
            Config.DB_PATH = original_db_path

        assert result["results"]
        assert result["results"][0]["body_main_text"] == "Cleaner retrieval text"
    finally:
        conn.close()
