#!/usr/bin/env python3
"""Inline reply salvage script."""

import hashlib
import json
import re
import sqlite3
import uuid
import logging
from pathlib import Path
from email_reply_parser import EmailReplyParser
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"
LOG_DIR = BASE_DIR / "ingestion" / "logs"
CHECKPOINT_PATH = BASE_DIR / "ingestion" / "resume_salvage.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"salvage_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def normalize_for_hash(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def compute_content_hash(text: str) -> str:
    normalized = normalize_for_hash(text)
    return hashlib.sha256(normalized.encode()).hexdigest()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        pass
    return conn


def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, 'r') as f:
            return json.load(f)
    return {"last_email_id": None, "processed": 0, "salvaged": 0, "duplicates": 0}


def save_checkpoint(state: dict):
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def salvage_quotes():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    checkpoint = load_checkpoint()
    
    query = """
        SELECT id, thread_id, subject, sender, recipients, COALESCE(body_text, body_markdown) AS body_text, embedding
        FROM emails 
        WHERE (source = 'original' OR source IS NULL)
          AND COALESCE(body_text, body_markdown) IS NOT NULL
          AND COALESCE(body_text, body_markdown) != ''
    """
    
    if checkpoint.get("last_email_id"):
        query += f" AND id > '{checkpoint['last_email_id']}'"
    
    query += " ORDER BY id LIMIT 1000"
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    logger.info(f"Processing {len(rows)} emails")
    
    for row in rows:
        email_id, thread_id, subject, sender, recipients, body_text, embedding = row
        
        try:
            fragments = EmailReplyParser.read(body_text)
            quoted_fragments = [f for f in fragments.fragments if f.quoted]
            
            for fragment in quoted_fragments:
                if not fragment.content or len(fragment.content.strip()) < 50:
                    continue
                
                content_hash = compute_content_hash(fragment.content)
                
                cursor.execute("SELECT id FROM emails WHERE content_hash = ?", (content_hash,))
                if cursor.fetchone():
                    checkpoint["duplicates"] += 1
                    continue
                
                salvaged_id = str(uuid.uuid4())
                
                cursor.execute("""
                    INSERT INTO emails (
                        id, thread_id, parent_id, source, content_hash,
                        timestamp, sender, recipients, subject, body_text,
                        category_tags, project_tags, is_outbound, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    salvaged_id, thread_id, email_id, "quoted_reply", content_hash,
                    datetime.now(timezone.utc).isoformat(), sender, recipients,
                    f"[Salvaged] {subject}", fragment.content, "", "", 0, embedding
                ))
                
                checkpoint["salvaged"] += 1
            
            checkpoint["last_email_id"] = email_id
            checkpoint["processed"] += 1
            
            if checkpoint["processed"] % 100 == 0:
                save_checkpoint(checkpoint)
                logger.info(f"Processed: {checkpoint['processed']}, Salvaged: {checkpoint['salvaged']}, Duplicates: {checkpoint['duplicates']}")
        
        except Exception as e:
            logger.error(f"Error processing {email_id}: {e}")
            continue
    
    conn.commit()
    conn.close()
    save_checkpoint(checkpoint)
    logger.info(f"Complete! Processed: {checkpoint['processed']}, Salvaged: {checkpoint['salvaged']}, Duplicates: {checkpoint['duplicates']}")


if __name__ == "__main__":
    salvage_quotes()
