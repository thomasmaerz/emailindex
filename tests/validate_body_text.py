#!/usr/bin/env python3
"""
Validate body_text column is properly populated.

This test ensures body_text is populated distinctly from markdown content
when markdown syntax is present.
"""

import sqlite3
import sys
from pathlib import Path
from typing import Tuple

import pytest

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "db" / "emails.db"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from migrate_body_text import markdown_contains_formatting, markdown_to_plain_text


def count_markdown_copies(conn: sqlite3.Connection) -> int:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT body_markdown, body_text
        FROM emails
        WHERE body_text IS NOT NULL
          AND body_markdown IS NOT NULL
          AND body_text = body_markdown
          AND body_markdown != ''
        """
    )

    markdown_copies = 0
    for body_markdown, body_text in cursor.fetchall():
        if not markdown_contains_formatting(body_markdown):
            continue
        if markdown_to_plain_text(body_markdown) == body_markdown:
            continue
        if body_text == body_markdown:
            markdown_copies += 1

    return markdown_copies


def get_db_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def validate_body_text() -> Tuple[bool, str]:
    if not DB_PATH.exists():
        return False, f"Database not found: {DB_PATH}"

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM emails")
    total = cursor.fetchone()[0]

    if total == 0:
        conn.close()
        return True, "No emails to validate"

    cursor.execute("""
        SELECT COUNT(*)
        FROM emails
        WHERE body_text IS NULL
          AND body_markdown IS NOT NULL
          AND body_markdown != ''
    """)
    null_body_text = cursor.fetchone()[0]

    markdown_copies = count_markdown_copies(conn)

    conn.close()

    if null_body_text > 0:
        return False, f"FAIL: {null_body_text}/{total} emails have NULL body_text with non-empty body_markdown"

    if markdown_copies > 0:
        return False, f"FAIL: {markdown_copies}/{total} emails still have body_text == body_markdown with markdown syntax"

    return True, f"PASS: {total}/{total} emails have body_text populated correctly"


@pytest.mark.skipif(not DB_PATH.exists(), reason=f"Database not found: {DB_PATH}")
def test_body_text_distinct_from_body_markdown():
    conn = sqlite3.connect(DB_PATH)
    count = count_markdown_copies(conn)
    conn.close()
    assert count == 0, f"{count} emails still have body_text == body_markdown with markdown syntax"


def main():
    passed, message = validate_body_text()
    print(message)

    if not passed:
        print("\nThis test verifies body_text contains clean plain text instead of markdown copies.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
