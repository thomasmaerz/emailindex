#!/usr/bin/env python3
"""One-time backfill for body_text/body_main_text and FTS5 migration to body_text."""

from __future__ import annotations

import sqlite3
import re
from pathlib import Path

from body_text_cleanup import derive_body_main_text, markdown_to_plain_text


ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "db" / "emails.db"

MARKDOWN_FORMATTING_PATTERNS = [
    r"(^|\n)#{1,6}\s+",
    r"!?\[.*?\]\([^\)]*\)",
    r"(^|\n)\s*[-*+]\s+",
    r"(^|\n)\s*\d+\.\s+",
    r"(^|\n)>\s+",
    r"(```|~~~)",
    r"`[^`]+`",
    r"\*\*[^*]+\*\*",
    r"__(?:[^_]+)__",
    r"(?<!\w)\*(?!\s)[^*\n]+\*(?!\w)",
    r"(?<!\w)_(?!\s)[^_\n]+_(?!\w)",
]


def markdown_contains_formatting(markdown_text: str | None) -> bool:
    if not markdown_text:
        return False
    return any(re.search(pattern, markdown_text, flags=re.MULTILINE) for pattern in MARKDOWN_FORMATTING_PATTERNS)


def fts_uses_body_text(cursor: sqlite3.Cursor) -> bool:
    cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'emails_fts'")
    row = cursor.fetchone()
    return bool(row and row[0] and "body_text" in row[0])


def drop_fts_objects(cursor: sqlite3.Cursor) -> None:
    cursor.execute("DROP TRIGGER IF EXISTS emails_fts_insert")
    cursor.execute("DROP TRIGGER IF EXISTS emails_fts_delete")
    cursor.execute("DROP TRIGGER IF EXISTS emails_fts_update")
    cursor.execute("DROP TABLE IF EXISTS emails_fts")


def create_fts_objects(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE VIRTUAL TABLE emails_fts USING fts5(
            subject,
            body_text,
            content='emails',
            content_rowid='rowid'
        )
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER emails_fts_insert AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, subject, body_text)
            VALUES (NEW.rowid, NEW.subject, NEW.body_text);
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER emails_fts_delete AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_text)
            VALUES('delete', OLD.rowid, OLD.subject, OLD.body_text);
        END
        """
    )
    cursor.execute(
        """
        CREATE TRIGGER emails_fts_update AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, body_text)
            VALUES('delete', OLD.rowid, OLD.subject, OLD.body_text);
            INSERT INTO emails_fts(rowid, subject, body_text)
            VALUES (NEW.rowid, NEW.subject, NEW.body_text);
        END
        """
    )


def backfill_body_text(conn: sqlite3.Connection, cursor: sqlite3.Cursor, batch_size: int = 1000) -> int:
    cursor.execute("SELECT COUNT(*) FROM emails")
    total = cursor.fetchone()[0]
    updated = 0
    last_rowid = 0

    while True:
        cursor.execute(
            """
            SELECT rowid, body_markdown, body_plain, body_text, body_main_text
            FROM emails
            WHERE rowid > ?
            ORDER BY rowid
            LIMIT ?
            """,
            (last_rowid, batch_size),
        )
        rows = cursor.fetchall()
        if not rows:
            break

        updates: list[tuple[str, str, int]] = []
        for rowid, body_markdown, body_plain, body_text, body_main_text in rows:
            plain_text = (body_text or "").strip()
            if not plain_text:
                has_markdown_formatting = markdown_contains_formatting(body_markdown)

                if has_markdown_formatting:
                    plain_text = markdown_to_plain_text(body_markdown)
                else:
                    plain_text = (body_markdown or "").strip()

                if not plain_text and body_plain:
                    plain_text = body_plain.strip()
                if not plain_text and body_markdown and not has_markdown_formatting:
                    plain_text = body_markdown.strip()

            main_text = (body_main_text or "").strip()
            if not main_text:
                main_text = derive_body_main_text(plain_text, body_markdown or "")

            updates.append((plain_text, main_text, rowid))
            last_rowid = rowid

        cursor.executemany("UPDATE emails SET body_text = ?, body_main_text = ? WHERE rowid = ?", updates)
        conn.commit()
        updated += len(updates)
        print(f"Backfilled {updated}/{total} emails", flush=True)

    return updated


def backfill_body_main_text(conn: sqlite3.Connection, cursor: sqlite3.Cursor, batch_size: int = 1000) -> int:
    return backfill_body_text(conn, cursor, batch_size=batch_size)


def rebuild_fts(cursor: sqlite3.Cursor) -> None:
    cursor.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA busy_timeout = 60000")
        cursor.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cursor.fetchall()}
        if "body_main_text" not in columns:
            cursor.execute("ALTER TABLE emails ADD COLUMN body_main_text TEXT")
            conn.commit()
        migrate_schema = not fts_uses_body_text(cursor)
        updated = backfill_body_text(conn, cursor)
        drop_fts_objects(cursor)
        create_fts_objects(cursor)
        rebuild_fts(cursor)
        conn.commit()
        print(f"Updated body_text for {updated} emails")
        print(f"FTS schema migrated: {migrate_schema}")
        print("FTS rebuilt successfully")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
