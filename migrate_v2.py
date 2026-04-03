#!/usr/bin/env python3
"""
Database migration script for emailindex v2 schema.
Adds new columns, FTS5 tables, triggers, and indexes.
"""

import sqlite3
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"

CATEGORY_KEYWORDS = {
    "scheduling": ["meeting", "calendar", "invite", "accepted", "declined", "tentative"],
    "security": ["security", "audit", "compliance", "nist", "cmmc", "assessment", "risk"],
    "infrastructure": ["server", "network", "exchange", "vmware", "domain join", "active directory"],
    "project_management": ["project", "pmo", "requirements", "deliverable", "milestone", "sprint", "scrum"],
    "finance": ["invoice", "payment", "budget", "cost", "purchase", "contract", "renewal"],
    "hr": ["benefits", "enrollment", "performance", "training", "onboarding", "pto"],
    "social": ["coffee", "conversation", "lunch", "birthday", "holiday", "celebration"],
    "vendor": ["demo", "trial", "license", "renewal", "contract", "proposal", "quote"],
    "system_notification": ["undeliverable", "bounce", "auto-reply", "out of office", "delivery status"],
}


def classify_email(subject: str, body: str) -> list[str]:
    subject = subject.lower() if subject else ""
    body = body[:1500].lower() if body else ""
    text_to_search = f"{subject} {body}"
    
    matched_categories = set()
    
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if " " in keyword:
                if keyword in text_to_search:
                    matched_categories.add(category)
                    break
            else:
                pattern = re.compile(r'\b' + re.escape(keyword) + r'\b')
                if pattern.search(text_to_search):
                    matched_categories.add(category)
                    break
    
    return sorted(list(matched_categories))

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception as e:
        print(f"Warning: sqlite-vec not available: {e}")
    
    new_columns = [
        ("parent_id", "TEXT"),
        ("source", "TEXT DEFAULT 'original'"),
        ("content_hash", "TEXT"),
        ("sender", "TEXT"),
        ("recipients", "TEXT"),
        ("body_text", "TEXT"),
        ("category_tags", "TEXT"),
        ("project_tags", "TEXT"),
        ("is_outbound", "INTEGER"),
    ]
    
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f"ALTER TABLE emails ADD COLUMN {col_name} {col_type}")
            print(f"Added column: {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                print(f"Column already exists: {col_name}")
            else:
                raise
    
    print("Migrating existing data...")
    cursor.execute("""
        UPDATE emails 
        SET sender = from_address,
            body_text = body_markdown,
            source = COALESCE(source, 'original')
        WHERE sender IS NULL
    """)
    print(f"Migrated {cursor.rowcount} rows")
    
    cursor.execute("""
        SELECT from_address FROM emails
        WHERE from_address IS NOT NULL AND from_address != ''
        GROUP BY from_address ORDER BY COUNT(*) DESC LIMIT 1
    """)
    row = cursor.fetchone()
    if row:
        owner = row[0]
        cursor.execute("""
            UPDATE emails SET is_outbound = CASE
                WHEN from_address = ? THEN 1 ELSE 0 END
            WHERE is_outbound IS NULL
        """, (owner,))
        print(f"Backfilled is_outbound for {cursor.rowcount} rows (owner: {owner})")
    else:
        print("No emails found to backfill is_outbound")
    
    print("Backfilling category_tags...")
    cursor.execute("SELECT id, subject, body_markdown FROM emails WHERE category_tags IS NULL OR category_tags = ''")
    rows = cursor.fetchall()
    tag_counts = {}
    batch_size = 500
    for i, row in enumerate(rows):
        email_id, subject, body_markdown = row
        tags = classify_email(subject, body_markdown or "")
        if tags:
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        cursor.execute("UPDATE emails SET category_tags = ? WHERE id = ?", (json.dumps(tags), email_id))
        if (i + 1) % batch_size == 0:
            conn.commit()
            print(f"  Processed {i + 1}/{len(rows)} emails...")
    conn.commit()
    print(f"Backfilled category_tags for {len(rows)} rows")
    if tag_counts:
        print(f"  Tag distribution: {tag_counts}")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_registry (
            name TEXT PRIMARY KEY,
            aliases TEXT,
            summary TEXT,
            created_at TEXT
        )
    """)
    print("Created project_registry table")
    
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS email_category_fts USING fts5(
            category_tags,
            project_tags,
            content='emails'
        )
    """)
    print("Created email_category_fts")
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
            INSERT INTO email_category_fts(rowid, category_tags, project_tags) 
            VALUES (new.rowid, new.category_tags, new.project_tags);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
            INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags) 
            VALUES('delete', old.rowid, old.category_tags, old.project_tags);
        END
    """)
    
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
            INSERT INTO email_category_fts(email_category_fts, rowid, category_tags, project_tags) 
            VALUES('delete', old.rowid, old.category_tags, old.project_tags);
            INSERT INTO email_category_fts(rowid, category_tags, project_tags) 
            VALUES (new.rowid, new.category_tags, new.project_tags);
        END
    """)
    print("Created FTS5 triggers")
    
    indexes = [
        ("idx_emails_content_hash", "CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_content_hash ON emails(content_hash)"),
        ("idx_emails_timestamp", "CREATE INDEX IF NOT EXISTS idx_emails_timestamp ON emails(timestamp)"),
        ("idx_emails_thread_id", "CREATE INDEX IF NOT EXISTS idx_emails_thread_id ON emails(thread_id)"),
        ("idx_emails_project_search", "CREATE INDEX IF NOT EXISTS idx_emails_project_search ON emails(timestamp, sender, category_tags)"),
    ]
    
    for idx_name, idx_sql in indexes:
        try:
            cursor.execute(idx_sql)
            print(f"Created index: {idx_name}")
        except sqlite3.OperationalError as e:
            if "already exists" in str(e).lower():
                print(f"Index already exists: {idx_name}")
            else:
                raise
    
    conn.commit()
    conn.close()
    print("Migration complete!")

if __name__ == "__main__":
    migrate()
