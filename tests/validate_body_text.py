#!/usr/bin/env python3
"""
Validate body_text column is properly populated.

This test ensures the fix for Issue #12 is in place:
- body_text should never be NULL when body_markdown is populated
- All newly ingested emails should have body_text = body_markdown
"""

import sqlite3
import sys
from pathlib import Path
from typing import Tuple

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "db" / "emails.db"


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

    cursor.execute("""
        SELECT COUNT(*)
        FROM emails
        WHERE body_text IS NOT NULL
          AND body_markdown IS NOT NULL
          AND body_text != body_markdown
    """)
    mismatched = cursor.fetchone()[0]

    conn.close()

    if null_body_text > 0:
        return False, f"FAIL: {null_body_text}/{total} emails have NULL body_text with non-empty body_markdown"

    if mismatched > 0:
        return False, f"FAIL: {mismatched}/{total} emails have body_text != body_markdown"

    return True, f"PASS: {total}/{total} emails have body_text populated correctly"


def main():
    passed, message = validate_body_text()
    print(message)

    if not passed:
        print("\nThis test verifies Issue #12 fix: body_text column must be populated for all emails.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()