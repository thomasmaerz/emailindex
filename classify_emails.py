#!/usr/bin/env python3
"""Batch classification script using Gemini for categorization and project tagging."""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import google.genai as genai
from google.genai import errors as genai_errors

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "emails.db"
LOG_DIR = BASE_DIR / "ingestion" / "logs"
CHECKPOINT_PATH = BASE_DIR / "ingestion" / "resume_classify.json"

BATCH_SIZE = int(os.environ.get("EMAILINDEX_BATCH_SIZE", "100"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"classify_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash-lite"
MAX_TOKENS_PER_CALL = 30000
BATCH_RETRIES = 5
BACKOFF_BASE = 1


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
            checkpoint = json.load(f)
        
        if checkpoint.get("last_email_id") and not checkpoint.get("last_timestamp"):
            logger.warning("Old checkpoint format detected (UUID-only cursor). Resetting cursor — starting fresh.")
            checkpoint["last_email_id"] = None
        
        if "last_timestamp" not in checkpoint:
            checkpoint["last_timestamp"] = None
        
        return checkpoint
    
    return {
        "discover_phase_done": False,
        "classify_phase_done": False,
        "last_email_id": None,
        "last_timestamp": None,
        "processed": 0,
        "projects_discovered": 0,
        "emails_classified": 0,
    }


def save_checkpoint(state: dict):
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_PATH, 'w') as f:
        json.dump(state, f, indent=2)


def call_gemini(prompt: str, max_retries: int = BATCH_RETRIES) -> Optional[str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            if response.text:
                return response.text
            logger.warning("Empty response from Gemini")
            return None
        except genai_errors.APIError as e:
            wait_time = BACKOFF_BASE * (2 ** attempt)
            logger.warning(f"Gemini API error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
        except Exception as e:
            logger.error(f"Unexpected error calling Gemini: {e}")
            return None

    logger.error(f"Failed after {max_retries} retries")
    return None


def discover_projects(checkpoint: dict) -> int:
    logger.info("Starting project discovery phase...")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT subject, COALESCE(NULLIF(body_text, ''), body_plain, body_markdown) AS body_text 
        FROM emails 
        WHERE COALESCE(NULLIF(body_text, ''), body_plain, body_markdown) IS NOT NULL AND COALESCE(NULLIF(body_text, ''), body_plain, body_markdown) != ''
        ORDER BY timestamp DESC
        LIMIT 500
    """)
    rows = cursor.fetchall()

    subjects = [r["subject"] for r in rows if r["body_text"] and len(r["body_text"]) > 100]
    bodies = [r["body_text"][:500] for r in rows if r["body_text"] and len(r["body_text"]) > 100]

    prompt = f"""Analyze these email subjects and body snippets to identify distinct projects, topics, or categories.
Return a JSON array of project names (max 20) with brief descriptions.

Subjects (sample):
{chr(10).join(subjects[:30])}

Body snippets (sample):
{chr(10).join(bodies[:10])}

Output format:
[
  {{"name": "ProjectName", "aliases": ["alias1", "alias2"], "summary": "Brief description"}}
]"""

    result = call_gemini(prompt)
    if not result:
        logger.error("Failed to discover projects")
        conn.close()
        return 0

    try:
        import re
        json_match = re.search(r'\[.*\]', result, re.DOTALL)
        if json_match:
            projects = json.loads(json_match.group())
        else:
            projects = json.loads(result)

        now = datetime.now(timezone.utc).isoformat()
        for proj in projects:
            aliases = ",".join(proj.get("aliases", []))
            cursor.execute("""
                INSERT OR REPLACE INTO project_registry (name, aliases, summary, created_at)
                VALUES (?, ?, ?, ?)
            """, (proj["name"], aliases, proj.get("summary", ""), now))

        conn.commit()
        discovered = len(projects)
        logger.info(f"Discovered {discovered} projects")
        checkpoint["discover_phase_done"] = True
        checkpoint["projects_discovered"] = discovered
        return discovered

    except Exception as e:
        logger.error(f"Failed to parse project discovery result: {e}")
        conn.close()
        return 0


def classify_emails(checkpoint: dict) -> int:
    logger.info("Starting classification phase...")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT name, aliases FROM project_registry")
    projects = cursor.fetchall()

    if not projects:
        logger.warning("No projects in registry, skipping classification")
        conn.close()
        return 0

    project_list = ", ".join([p[0] for p in projects])

    query = """
        SELECT id, subject, COALESCE(NULLIF(body_text, ''), body_plain, body_markdown) AS body_text, sender, recipients, timestamp
        FROM emails
        WHERE category_tags IS NULL OR category_tags = ''
    """

    params = ()
    if checkpoint.get("last_timestamp"):
        query += " AND (timestamp > ? OR (timestamp = ? AND id > ?))"
        params = (checkpoint["last_timestamp"], checkpoint["last_timestamp"], checkpoint["last_email_id"])

    query += " ORDER BY timestamp ASC, id ASC LIMIT ?"
    params = (*params, BATCH_SIZE)

    cursor.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        logger.info("No emails to classify")
        conn.close()
        return 0

    email_batch = []
    for row in rows:
        email_batch.append({
            "id": row["id"],
            "subject": row["subject"],
            "body": (row["body_text"] or "")[:1000],
            "sender": row["sender"],
        })

    prompt = f"""Classify each email with category tags and project tags.
Categories: work, personal, financial, travel, newsletter, automated, meeting, misc
Projects: {project_list}

Emails:
{json.dumps(email_batch, indent=2)}

Output a JSON array with:
[
  {{"id": "email_id", "category_tags": "category1,category2", "project_tags": "project1,project2"}}
]"""

    result = call_gemini(prompt)
    if not result:
        logger.error("Failed to classify emails")
        conn.close()
        return 0

    classified = 0
    try:
        import re
        json_match = re.search(r'\[.*\]', result, re.DOTALL)
        if json_match:
            classifications = json.loads(json_match.group())
        else:
            classifications = json.loads(result)

        for cls in classifications:
            existing_tags_raw = cls.get("category_tags", "")
            new_project_tags_raw = cls.get("project_tags", "")
            
            cursor.execute("SELECT category_tags FROM emails WHERE id = ?", (cls.get("id"),))
            row = cursor.fetchone()
            existing_category_tags = []
            if row and row["category_tags"]:
                try:
                    existing_category_tags = json.loads(row["category_tags"])
                except json.JSONDecodeError:
                    existing_category_tags = []
            
            new_category_tags = []
            if existing_tags_raw:
                new_tags = [t.strip() for t in existing_tags_raw.split(",") if t.strip()]
                new_category_tags = list(set(existing_category_tags + new_tags))
            
            merged_category_tags = json.dumps(sorted(new_category_tags)) if new_category_tags else json.dumps([])
            
            new_project_tags = []
            if new_project_tags_raw:
                new_project_tags = [t.strip() for t in new_project_tags_raw.split(",") if t.strip()]
            project_tags_json = json.dumps(sorted(new_project_tags)) if new_project_tags else "[]"
            
            cursor.execute("""
                UPDATE emails 
                SET category_tags = ?, project_tags = ?
                WHERE id = ?
            """, (merged_category_tags, project_tags_json, cls.get("id")))

        conn.commit()
        classified = len(classifications)
        logger.info(f"Classified {classified} emails")
        checkpoint["emails_classified"] += classified

        if rows:
            checkpoint["last_timestamp"] = rows[-1]["timestamp"]
            checkpoint["last_email_id"] = rows[-1]["id"]

    except Exception as e:
        logger.error(f"Failed to parse classification result: {e}")

    conn.close()
    return classified


def run_classification():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint()

    if not checkpoint.get("discover_phase_done"):
        discover_projects(checkpoint)
        save_checkpoint(checkpoint)

    classified_count = 0
    while True:
        count = classify_emails(checkpoint)
        if count == 0:
            break
        classified_count += count
        save_checkpoint(checkpoint)

    checkpoint["classify_phase_done"] = True
    save_checkpoint(checkpoint)
    logger.info(f"Classification complete! Total classified: {classified_count}")


if __name__ == "__main__":
    run_classification()
