#!/usr/bin/env python3
"""
Re-salvage quoted replies from existing emails without re-ingestion.
Processes all original emails in the database, extracts quoted reply fragments
using the updated salvage_quotes() function (including Outlook pattern detection),
generates embeddings for salvaged records, and inserts them into the database.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"
LOG_DIR = BASE_DIR / "ingestion" / "logs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"resalvage_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(BASE_DIR))

from ingest import (
    salvage_quotes,
    get_db_connection,
    insert_email,
    generate_embedding_text,
    Embedder,
)


def re_salvage_quotes(batch_size: int = 8, dry_run: bool = False):
    """Re-process existing emails to extract quoted replies."""
    conn = get_db_connection(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("""
        SELECT * FROM emails 
        WHERE source = 'original' 
          AND body_plain IS NOT NULL 
          AND body_plain != ''
        ORDER BY timestamp
    """)
    originals = c.fetchall()
    total = len(originals)
    
    logger.info(f"Found {total} original emails with body_plain content")
    
    processed = 0
    salvaged_count = 0
    errors = 0
    pending_records = []
    pending_texts = []
    
    embedder = Embedder()
    
    for email in originals:
        try:
            parent = dict(email)
            records = salvage_quotes(parent['body_plain'], parent, conn)
            
            if not records:
                processed += 1
                continue
            
            for record in records:
                text = generate_embedding_text(record)
                pending_records.append(record)
                pending_texts.append(text)
            
            if len(pending_records) >= batch_size:
                embeddings = embedder.encode_batch(pending_texts)
                
                for rec, emb in zip(pending_records, embeddings):
                    rec['embedding'] = emb
                    if not dry_run:
                        insert_email(conn, rec)
                    salvaged_count += 1
                
                if not dry_run:
                    conn.commit()
                
                pending_records = []
                pending_texts = []
            
            processed += 1
            if processed % 500 == 0:
                logger.info(f"Processed: {processed}/{total}, Salvaged: {salvaged_count}")
        
        except Exception as e:
            errors += 1
            logger.warning(f"Error processing email {email['id'][:8]}: {e}")
            continue
    
    if pending_records:
        embeddings = embedder.encode_batch(pending_texts)
        for rec, emb in zip(pending_records, embeddings):
            rec['embedding'] = emb
            if not dry_run:
                insert_email(conn, rec)
            salvaged_count += 1
        
        if not dry_run:
            conn.commit()
    
    conn.close()
    
    logger.info(f"Re-salvage complete:")
    logger.info(f"  Processed: {processed}")
    logger.info(f"  Salvaged: {salvaged_count}")
    logger.info(f"  Errors: {errors}")
    
    return {
        'processed': processed,
        'salvaged': salvaged_count,
        'errors': errors,
    }


def main():
    parser = argparse.ArgumentParser(description="Re-salvage quoted replies from existing emails")
    parser.add_argument("--batch-size", type=int, default=8, help="Embedding batch size")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB, just report")
    args = parser.parse_args()
    
    if not DB_PATH.exists():
        logger.error(f"Database not found: {DB_PATH}")
        sys.exit(1)
    
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    stats = re_salvage_quotes(batch_size=args.batch_size, dry_run=args.dry_run)
    
    if stats['salvaged'] == 0:
        logger.warning("No quoted replies salvaged. Check if emails have quote patterns.")
        sys.exit(1)
    
    sys.exit(0)


if __name__ == "__main__":
    main()
