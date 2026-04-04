#!/usr/bin/env python3
"""
Shared cleanup utilities for Email Intelligence System tests.

Provides functions to clean up test artifacts:
- Old validation log files
- Orphaned attachment files (on disk but not in DB)
- Stray database files (worktree-aware)
"""

import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Tuple

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "ingestion" / "logs"
DB_DIR = BASE_DIR / "db"
ATTACHMENTS_DIR = BASE_DIR / "attachments"
WORKTREES_DIR = BASE_DIR / ".worktrees"
MAIN_DB = DB_DIR / "emails.db"


def cleanup_old_logs(days: int = 7, dry_run: bool = False) -> Tuple[int, int]:
    """Delete validation log files older than `days` days.

    Returns (removed_count, freed_bytes).
    """
    if not LOG_DIR.exists():
        return 0, 0

    cutoff = time.time() - (days * 86400)
    removed = 0
    freed = 0

    for log_file in LOG_DIR.iterdir():
        if log_file.is_file() and log_file.name.startswith("validation_") and log_file.name.endswith(".log"):
            if log_file.stat().st_mtime < cutoff:
                size = log_file.stat().st_size
                if not dry_run:
                    log_file.unlink()
                removed += 1
                freed += size

    return removed, freed


def cleanup_log_dir_size_cap(max_bytes: int = 50 * 1024 * 1024, dry_run: bool = False) -> Tuple[int, int]:
    """Delete oldest log files until total log directory is under max_bytes.

    Returns (removed_count, freed_bytes).
    """
    if not LOG_DIR.exists():
        return 0, 0

    log_files = sorted(
        [f for f in LOG_DIR.iterdir() if f.is_file() and f.name.startswith("validation_") and f.name.endswith(".log")],
        key=lambda f: f.stat().st_mtime
    )

    total_size = sum(f.stat().st_size for f in log_files)
    removed = 0
    freed = 0

    while total_size > max_bytes and log_files:
        oldest = log_files.pop(0)
        size = oldest.stat().st_size
        if not dry_run:
            oldest.unlink()
        removed += 1
        freed += size
        total_size -= size

    return removed, freed


def cleanup_orphaned_attachments(dry_run: bool = False) -> Tuple[int, int]:
    """Remove attachment files on disk that are not referenced in attachment_hashes table.

    Returns (removed_count, freed_bytes).
    """
    if not ATTACHMENTS_DIR.exists() or not MAIN_DB.exists():
        return 0, 0

    conn = sqlite3.connect(MAIN_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT path FROM attachment_hashes")
    db_paths = set()
    for row in cursor.fetchall():
        path = row[0].replace("attachments/", "").replace("attachments\\", "")
        db_paths.add(path)
    conn.close()

    removed = 0
    freed = 0

    for root, dirs, files in os.walk(ATTACHMENTS_DIR):
        for filename in files:
            full_path = Path(root) / filename
            rel_path = str(full_path.relative_to(ATTACHMENTS_DIR)).replace("//", "/")
            if rel_path not in db_paths:
                size = full_path.stat().st_size
                if not dry_run:
                    full_path.unlink()
                removed += 1
                freed += size

    return removed, freed


def cleanup_temp_dbs(dry_run: bool = False) -> Tuple[int, int, list]:
    """Remove stray .db files in db/ that aren't the main emails.db.

    Worktree databases are NOT deleted — they are legitimate copies belonging
    to active git worktrees. Their sizes are reported but they are left alone.

    Returns (removed_count, freed_bytes, [worktree_db_info_strings]).
    """
    removed = 0
    freed = 0
    worktree_info = []

    # Report worktree databases (do NOT delete)
    if WORKTREES_DIR.exists():
        for worktree in WORKTREES_DIR.iterdir():
            if worktree.is_dir():
                wt_db = worktree / "db" / "emails.db"
                if wt_db.exists():
                    size_mb = wt_db.stat().st_size / (1024 * 1024)
                    worktree_info.append(f"  {worktree.name}/db/emails.db ({size_mb:.1f} MB)")

    # Clean stray .db files in main db/ directory
    if DB_DIR.exists():
        for db_file in DB_DIR.iterdir():
            if db_file.is_file() and db_file.suffix == ".db" and db_file != MAIN_DB:
                size = db_file.stat().st_size
                if not dry_run:
                    db_file.unlink()
                removed += 1
                freed += size

    return removed, freed, worktree_info


def run_all_cleanup(days: int = 7, dry_run: bool = False) -> dict:
    """Run all cleanup tasks and return a summary dict."""
    log_removed, log_freed = cleanup_old_logs(days=days, dry_run=dry_run)
    cap_removed, cap_freed = cleanup_log_dir_size_cap(dry_run=dry_run)
    att_removed, att_freed = cleanup_orphaned_attachments(dry_run=dry_run)
    db_removed, db_freed, wt_info = cleanup_temp_dbs(dry_run=dry_run)

    return {
        "logs_removed_by_age": log_removed,
        "logs_removed_by_cap": cap_removed,
        "logs_freed_bytes": log_freed + cap_freed,
        "attachments_removed": att_removed,
        "attachments_freed_bytes": att_freed,
        "dbs_removed": db_removed,
        "dbs_freed_bytes": db_freed,
        "worktree_dbs": wt_info,
        "total_freed_bytes": log_freed + cap_freed + att_freed + db_freed,
    }