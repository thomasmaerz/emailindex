#!/usr/bin/env python3
"""
Attachment Pipeline Validation Script

Validates the end-to-end attachment pipeline:
1. Parses maildir .eml files and detects attachment MIME parts
2. Compares against DB has_attachments column
3. Verifies attachment_hashes table paths exist on disk
4. Finds orphaned files on disk not referenced by DB
"""

import argparse
import email
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from tests.cleanup import cleanup_orphaned_attachments


def get_db_path() -> Path:
    base = Path(__file__).parent.parent
    return base / "db" / "emails.db"


def get_attachments_dir() -> Path:
    base = Path(__file__).parent.parent
    return base / "attachments"


def get_maildir_path() -> Path:
    base = Path(__file__).parent.parent
    return base / "maildir"


def parse_email_attachments(eml_path: Path) -> tuple[int, list[str]]:
    """
    Parse an .eml file and return count of attachment-like MIME parts.
    Returns (attachment_count, list of filenames)
    """
    try:
        with open(eml_path, 'rb') as f:
            raw_bytes = f.read()
        message = email.message_from_bytes(raw_bytes)
    except Exception:
        return 0, []

    content_ids = set()
    for part in message.walk():
        cid = part.get('Content-ID')
        if cid:
            content_ids.add(cid.strip('<>'))

    html_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True)
                if payload and isinstance(payload, bytes):
                    try:
                        charset = part.get_content_charset() or 'utf-8'
                        html_body = payload.decode(charset, errors='replace')
                    except Exception:
                        pass
                elif isinstance(payload, str):
                    html_body = payload
                break
    else:
        if message.get_content_type() == 'text/html':
            payload = message.get_payload(decode=True)
            if payload and isinstance(payload, bytes):
                try:
                    charset = message.get_content_charset() or 'utf-8'
                    html_body = payload.decode(charset, errors='replace')
                except Exception:
                    pass
            elif isinstance(payload, str):
                html_body = payload

    cid_refs = set(re.findall(r'cid:([^"\'>\s]+)', html_body))

    attachment_count = 0
    filenames = []

    for part in message.walk():
        content_disposition = part.get('Content-Disposition', '')
        disposition_lower = content_disposition.lower().strip()

        if not disposition_lower:
            continue

        filename = part.get_filename()
        if not filename:
            continue

        is_attachment = 'attachment' in disposition_lower
        is_inline = 'inline' in disposition_lower

        if not is_attachment and not is_inline:
            continue

        if is_inline and not is_attachment:
            cid = part.get('Content-ID', '').strip('<>')
            if cid in cid_refs:
                continue

        attachment_count += 1
        filenames.append(filename)

    return attachment_count, filenames


def scan_maildir() -> dict:
    """Scan all .eml files in maildir and collect attachment stats."""
    maildir = get_maildir_path()
    results = {
        'total': 0,
        'with_attachment': 0,
        'with_inline': 0,
        'without': 0,
        'discrepancies': []
    }

    for root, dirs, files in os.walk(maildir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith('.eml') or (not file.startswith('.') and not file.endswith('.msf') and not file.endswith('.dat')):
                eml_path = Path(root) / file
                count, _ = parse_email_attachments(eml_path)
                results['total'] += 1
                if count > 0:
                    results['with_attachment'] += 1
                else:
                    results['without'] += 1

    return results


def get_db_stats() -> dict:
    """Get attachment-related stats from the database."""
    db_path = get_db_path()
    if not db_path.exists():
        return {'error': 'Database not found'}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stats = {}

    cursor.execute("SELECT has_attachments, COUNT(*) as count FROM emails GROUP BY has_attachments")
    for row in cursor.fetchall():
        stats[f'has_attachments_{row["has_attachments"]}'] = row['count']

    cursor.execute("SELECT COUNT(*) as count FROM emails WHERE attachments != '[]' AND attachments IS NOT NULL")
    stats['with_attachments_json'] = cursor.fetchone()['count']

    cursor.execute("SELECT COUNT(*) as count FROM attachment_hashes")
    stats['attachment_hashes_count'] = cursor.fetchone()['count']

    cursor.execute("SELECT id, has_attachments, attachments FROM emails WHERE has_attachments = 1")
    stats['has_1_records'] = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT id, subject FROM emails WHERE has_attachments = 0 AND attachments != '[]' AND attachments IS NOT NULL")
    stats['has_0_with_json'] = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return stats


def get_disk_attachments() -> set:
    """Get all attachment file paths from disk."""
    attachments_dir = get_attachments_dir()
    paths = set()

    if not attachments_dir.exists():
        return paths

    for root, dirs, files in os.walk(attachments_dir):
        for file in files:
            full_path = Path(root) / file
            rel_path = full_path.relative_to(attachments_dir)
            normalized = str(rel_path).replace('//', '/')
            paths.add(normalized)

    return paths


def get_db_attachment_paths() -> set:
    """Get all attachment paths from attachment_hashes table."""
    db_path = get_db_path()
    if not db_path.exists():
        return set()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    paths = set()
    cursor.execute("SELECT path FROM attachment_hashes")
    for row in cursor.fetchall():
        path = row[0]
        path = path.replace('attachments/', '')
        path = path.replace('attachments\\', '')
        paths.add(path)

    conn.close()
    return paths


def validate(has_attachments_fix: bool = False, cleanup: bool = False) -> bool:
    """Run the full validation and return True if all checks pass."""

    print("=" * 60)
    print("=== Attachment Pipeline Validation ===")
    print("=" * 60)

    print("\n[1] Scanning maildir...")
    maildir_results = scan_maildir()
    print(f"    Maildir emails scanned: {maildir_results['total']}")
    print(f"    - With attachments (attachment disposition): {maildir_results['with_attachment']}")
    print(f"    - Without attachment parts: {maildir_results['without']}")

    print("\n[2] Getting database stats...")
    db_stats = get_db_stats()
    if 'error' in db_stats:
        print(f"    ERROR: {db_stats['error']}")
        return False

    has_1_count = db_stats.get('has_attachments_1', 0)
    has_0_count = db_stats.get('has_attachments_0', 0)
    print(f"    - has_attachments=1: {has_1_count}")
    print(f"    - has_attachments=0: {has_0_count}")

    print("\n[3] Checking DB consistency...")
    inconsistent = len(db_stats.get('has_0_with_json', []))
    if inconsistent > 0:
        print(f"    WARNING: {inconsistent} emails have has_attachments=0 but non-empty attachments JSON")
        for rec in db_stats['has_0_with_json'][:5]:
            print(f"      - {rec['id']}: {rec['subject'][:40]}")
    else:
        print("    OK: has_attachments matches attachments JSON")

    print("\n[4] Comparing maildir vs DB...")
    maildir_with_att = maildir_results['with_attachment']
    db_with_att = has_1_count

    if has_attachments_fix and maildir_with_att != db_with_att:
        discrepancy = maildir_with_att - db_with_att
        print(f"    DISCREPANCY: Maildir has {maildir_with_att}, DB has {db_with_att} (diff: {discrepancy})")
        print("    NOTE: This is expected if ingestion needs to be re-run after fixes")
    elif maildir_with_att == db_with_att:
        print(f"    OK: Both maildir and DB report {maildir_with_att} emails with attachments")
    else:
        print(f"    WARNING: Maildir has {maildir_with_att}, DB has {db_with_att}")

    print("\n[5] Checking attachment files on disk vs DB...")
    disk_paths = get_disk_attachments()
    db_paths = get_db_attachment_paths()

    on_disk_not_db = disk_paths - db_paths
    on_db_not_disk = db_paths - disk_paths

    print(f"    - Files on disk: {len(disk_paths)}")
    print(f"    - Paths in DB: {len(db_paths)}")
    print(f"    - On disk but not in DB: {len(on_disk_not_db)}")
    print(f"    - In DB but not on disk: {len(on_db_not_disk)}")

    if on_disk_not_db:
        print("\n    Sample orphaned files (on disk, not in DB):")
        for p in list(on_disk_not_db)[:10]:
            print(f"      {p}")

    if on_db_not_disk:
        print("\n    WARNING: Missing files (in DB, not on disk):")
        for p in list(on_db_not_disk)[:10]:
            print(f"      {p}")

    print("\n" + "=" * 60)

    all_ok = (
        inconsistent == 0 and
        len(on_disk_not_db) == 0 and
        len(on_db_not_disk) == 0
    )

    if has_attachments_fix:
        all_ok = all_ok and (maildir_with_att == db_with_att)

    if all_ok:
        print("=== Result: PASS ===")
    else:
        print("=== Result: FAIL ===")
        if not has_attachments_fix:
            print("\nNOTE: Run with --has-attachments-fix to see expected state after re-ingestion")

    print("=" * 60)

    if cleanup and on_disk_not_db:
        print("\n[6] Cleanup orphaned files...")
        attachments_dir = get_attachments_dir()
        removed = 0
        for rel_path in on_disk_not_db:
            full_path = attachments_dir / rel_path
            try:
                full_path.unlink()
                removed += 1
            except Exception as e:
                print(f"    Failed to remove {rel_path}: {e}")
        print(f"    Removed {removed} orphaned files")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Validate attachment pipeline")
    parser.add_argument('--has-attachments-fix', action='store_true',
                        help='Show expected state after fixes are applied')
    parser.add_argument('--cleanup', action='store_true',
                        help='Remove orphaned files from attachments/ directory (manual)')
    parser.add_argument('--auto-cleanup', action='store_true',
                        help='Automatically remove orphaned files during validation')
    args = parser.parse_args()

    if args.auto_cleanup:
        print("\n[Auto-cleanup] Removing orphaned attachment files...")
        removed, freed = cleanup_orphaned_attachments(dry_run=False)
        freed_mb = freed / (1024 * 1024)
        print(f"[Auto-cleanup] Removed {removed} orphaned files ({freed_mb:.1f} MB freed)")

    result = validate(has_attachments_fix=args.has_attachments_fix, cleanup=args.cleanup)
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()