#!/usr/bin/env python3
"""
Validation script for GitHub Issue #2: body_markdown contains raw HTML

Traces the full pipeline: maildir/.eml -> parse_email_file() -> SQLite db/emails.db
Classifies each email and confirms the root cause of raw HTML leaking into body_markdown.

Usage:
    python3 tests/validate_issue2.py              # full scan (995 emails)
    python3 tests/validate_issue2.py --sample 50  # quick scan
    python3 tests/validate_issue2.py --verbose    # per-email details
"""

import argparse
import email
import email.policy
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
MAILDIR = BASE_DIR / "maildir" / "cur"
DB_PATH = BASE_DIR / "db" / "emails.db"

RAW_HTML_MARKERS = [
    "<html", "<HTML", "<!DOCTYPE", "<!doctype",
    "<head", "<HEAD", "<body", "<BODY",
    "<meta ", "<META ",
]


def is_raw_html(text):
    if not text:
        return False
    head = text[:500].lower()
    return any(marker.lower() in head for marker in RAW_HTML_MARKERS)


def extract_bodies_from_eml(eml_path):
    """Parse an .eml file and extract body information."""
    with open(eml_path, "rb") as f:
        raw = f.read()

    msg = email.message_from_bytes(raw, policy=email.policy.default)

    result = {
        "message_id": (msg.get("Message-ID") or "").strip().strip("<>"),
        "is_multipart": msg.is_multipart(),
        "html_body": "",
        "plain_body": "",
        "content_types": [],
        "part_count": 0,
        "single_part_type": None,
    }

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            result["part_count"] += 1
            if ct not in result["content_types"]:
                result["content_types"].append(ct)

            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        result["html_body"] = payload.decode(charset, errors="replace")
                    except Exception:
                        result["html_body"] = payload.decode("utf-8", errors="replace")
            elif ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        result["plain_body"] = payload.decode(charset, errors="replace")
                    except Exception:
                        result["plain_body"] = payload.decode("utf-8", errors="replace")
    else:
        ct = msg.get_content_type()
        result["content_types"].append(ct)
        result["single_part_type"] = ct
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")

            if ct == "text/html":
                result["html_body"] = decoded
            elif ct == "text/plain":
                result["plain_body"] = decoded
            else:
                result["plain_body"] = decoded

    return result


def query_db_by_message_id(conn, message_id):
    """Query the DB for a record matching the Message-ID."""
    cursor = conn.cursor()

    for mid in [message_id, "<{}>".format(message_id)]:
        cursor.execute(
            "SELECT id, message_id, body_markdown, body_plain, folder FROM emails WHERE message_id = ?",
            (mid,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "message_id": row["message_id"],
                "body_markdown": row["body_markdown"],
                "body_plain": row["body_plain"],
                "folder": row["folder"],
            }

    return None


def classify_email(eml_info, db_record):
    """Classify an email into a category."""
    if db_record is None:
        return "NO_DB_RECORD"

    md = db_record["body_markdown"] or ""
    pl = db_record["body_plain"] or ""

    if not md and not pl:
        return "EMPTY"

    if is_raw_html(md):
        return "RAW_HTML_LEAK"

    if md == pl and pl:
        return "PLAIN_ONLY"

    return "CLEAN_MARKDOWN"


def run_validation(sample_size=None, verbose=False):
    """Main validation logic."""
    if not MAILDIR.exists():
        print("=" * 60)
        print("  Issue #2 Validation Report")
        print("=" * 60)
        print("\nSKIP (maildir/cur/ not found)")
        print("=" * 60)
        print("  Result: SKIP")
        print("=" * 60)
        sys.exit(0)

    maildir_files = [f for f in MAILDIR.iterdir() if f.is_file() and not f.name.startswith('.')]
    if not maildir_files:
        print("=" * 60)
        print("  Issue #2 Validation Report")
        print("=" * 60)
        print("\nSKIP (maildir/cur/ is empty)")
        print("=" * 60)
        print("  Result: SKIP")
        print("=" * 60)
        sys.exit(0)

    if not DB_PATH.exists():
        print("ERROR: Database not found: {}".format(DB_PATH))
        sys.exit(1)

    eml_files = sorted(MAILDIR.iterdir())
    if sample_size:
        eml_files = eml_files[:sample_size]

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    categories = {
        "CLEAN_MARKDOWN": 0,
        "RAW_HTML_LEAK": 0,
        "PLAIN_ONLY": 0,
        "EMPTY": 0,
        "NO_DB_RECORD": 0,
        "UNKNOWN": 0,
    }

    root_cause = {
        "single_part_html_with_leak": 0,
        "multipart_with_leak": 0,
        "single_part_plain_with_leak": 0,
        "other_leak": 0,
    }

    raw_html_samples = []
    no_record_samples = []

    for eml_path in eml_files:
        if not eml_path.is_file():
            continue

        eml_info = extract_bodies_from_eml(eml_path)
        db_record = query_db_by_message_id(conn, eml_info["message_id"])
        category = classify_email(eml_info, db_record)
        categories[category] += 1

        if category == "RAW_HTML_LEAK":
            if eml_info["single_part_type"] == "text/html":
                root_cause["single_part_html_with_leak"] += 1
            elif eml_info["is_multipart"]:
                root_cause["multipart_with_leak"] += 1
            elif eml_info["single_part_type"] == "text/plain":
                root_cause["single_part_plain_with_leak"] += 1
            else:
                root_cause["other_leak"] += 1

            if len(raw_html_samples) < 10:
                raw_html_samples.append((eml_path, eml_info, db_record))

        if category == "NO_DB_RECORD" and len(no_record_samples) < 5:
            no_record_samples.append((eml_path, eml_info))

        if verbose:
            print("\n--- {} ---".format(eml_path.name))
            print("  Message-ID: {}".format(eml_info["message_id"][:60]))
            print("  Multipart: {}, Parts: {}".format(eml_info["is_multipart"], eml_info["part_count"]))
            print("  Content types: {}".format(eml_info["content_types"]))
            if eml_info["single_part_type"]:
                print("  Single-part type: {}".format(eml_info["single_part_type"]))
            print("  HTML body: {} chars".format(len(eml_info["html_body"])))
            print("  Plain body: {} chars".format(len(eml_info["plain_body"])))
            if db_record:
                print("  DB record: {}".format(db_record["id"][:8]))
                print("  body_markdown: {} chars".format(len(db_record["body_markdown"] or "")))
                print("  body_plain: {} chars".format(len(db_record["body_plain"] or "")))
            else:
                print("  DB record: NOT FOUND")
            print("  Category: {}".format(category))

    conn.close()

    total = sum(categories.values())

    print("=" * 60)
    print("  Issue #2 Validation Report")
    print("=" * 60)
    print("\nTotal emails analyzed: {}".format(total))
    print("  {:<30} {:>5} ({:.1f}%)".format("Clean Markdown", categories["CLEAN_MARKDOWN"], categories["CLEAN_MARKDOWN"]/total*100 if total else 0))
    print("  {:<30} {:>5} ({:.1f}%)".format("Raw HTML in body_markdown", categories["RAW_HTML_LEAK"], categories["RAW_HTML_LEAK"]/total*100 if total else 0))
    print("  {:<30} {:>5} ({:.1f}%)".format("Plain-only (no HTML source)", categories["PLAIN_ONLY"], categories["PLAIN_ONLY"]/total*100 if total else 0))
    print("  {:<30} {:>5} ({:.1f}%)".format("Empty", categories["EMPTY"], categories["EMPTY"]/total*100 if total else 0))
    print("  {:<30} {:>5}".format("No DB record", categories["NO_DB_RECORD"]))
    if categories["UNKNOWN"]:
        print("  {:<30} {:>5}".format("Unknown", categories["UNKNOWN"]))

    print("\n" + "-" * 60)
    print("  Root Cause Analysis (for raw HTML leaks)")
    print("-" * 60)
    leak_total = categories["RAW_HTML_LEAK"]
    if leak_total > 0:
        print("\n  Single-part text/html -> raw HTML leak:  {:>5} ({:.1f}%)".format(
            root_cause["single_part_html_with_leak"],
            root_cause["single_part_html_with_leak"]/leak_total*100 if leak_total else 0))
        print("  Multipart -> raw HTML leak:              {:>5}".format(root_cause["multipart_with_leak"]))
        print("  Single-part text/plain -> raw HTML leak: {:>5}".format(root_cause["single_part_plain_with_leak"]))
        print("  Other -> raw HTML leak:                  {:>5}".format(root_cause["other_leak"]))

        if root_cause["single_part_html_with_leak"] == leak_total:
            print("\n  CONFIRMED: All {} raw HTML leaks are from single-part text/html emails.".format(leak_total))
            print("  The bug is in EncodingHandler.get_message_body() - it only checks")
            print("  content type for multipart messages, not single-part.")
        else:
            print("\n  Mixed root causes - further investigation needed.")
    else:
        print("\n  No raw HTML leaks detected. Issue #2 may already be fixed.")

    if raw_html_samples:
        print("\n" + "-" * 60)
        print("  Sample: Raw HTML Leak Cases")
        print("-" * 60)

        for eml_path, eml_info, db_record in raw_html_samples[:5]:
            print("\n  Email: {}".format(eml_path.name))
            print("  Source: single-part {}, HTML size: {} chars".format(
                eml_info["single_part_type"], len(eml_info["html_body"])))
            print("  DB body_markdown ({} chars):".format(len(db_record["body_markdown"] or "")))
            md_preview = (db_record["body_markdown"] or "")[:150]
            print("    {}...".format(md_preview))
            print("  DB body_plain ({} chars):".format(len(db_record["body_plain"] or "")))
            pl_preview = (db_record["body_plain"] or "")[:150]
            print("    {}...".format(pl_preview))
            print("  Are they equal? {}".format(db_record["body_markdown"] == db_record["body_plain"]))

    if no_record_samples:
        print("\n" + "-" * 60)
        print("  Sample: Emails Without DB Records")
        print("-" * 60)
        for eml_path, eml_info in no_record_samples:
            print("  {} - Message-ID: {}".format(eml_path.name, eml_info["message_id"][:60]))

    print("\n" + "=" * 60)

    return categories


def main():
    parser = argparse.ArgumentParser(description="Validate Issue #2: body_markdown contains raw HTML")
    parser.add_argument("--sample", type=int, default=None, help="Limit to N emails (default: all)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print per-email details")
    args = parser.parse_args()

    run_validation(sample_size=args.sample, verbose=args.verbose)


if __name__ == "__main__":
    main()
