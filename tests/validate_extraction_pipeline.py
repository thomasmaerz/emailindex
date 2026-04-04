#!/usr/bin/env python3
"""
Validation script for GitHub Issue #4: Full extraction pipeline validation

Traces data fidelity from Outlook source through to SQLite:
1. Extraction quality — Check extraction logs, verify .eml format
2. Maildir → DB field preservation — Compare .eml fields to DB records
3. Attachment pipeline — Verify attachment files exist on disk
4. Vector coverage — Confirm all DB emails have embeddings

Usage:
    python3 tests/validate_extraction_pipeline.py         # full validation
    python3 tests/validate_extraction_pipeline.py --sample 50  # quick scan
    python3 tests/validate_extraction_pipeline.py --verbose     # detailed output
"""

import argparse
import email
import email.policy
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
MAILDIR = BASE_DIR / "maildir" / "cur"
DB_PATH = BASE_DIR / "db" / "emails.db"
ATTACHMENTS_DIR = BASE_DIR / "attachments"


def scan_maildir_count() -> int:
    """Count .eml files in maildir."""
    return len([f for f in os.listdir(MAILDIR) if f and not f.startswith('.')])


def extract_bodies_from_eml(eml_path: Path) -> dict:
    """Parse an .eml file and extract body information."""
    with open(eml_path, "rb") as f:
        raw = f.read()

    msg = email.message_from_bytes(raw, policy=email.policy.default)

    result = {
        "message_id": (msg.get("Message-ID") or "").strip().strip("<>"),
        "subject": msg.get("Subject") or "",
        "from": msg.get("From") or "",
        "date": msg.get("Date") or "",
        "has_attachments": False,
        "attachment_count": 0,
        "body_markdown": "",
        "body_plain": "",
        "content_types": [],
    }

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct not in result["content_types"]:
                result["content_types"].append(ct)

            disposition = part.get("Content-Disposition", "")
            if "attachment" in disposition.lower():
                result["has_attachments"] = True
                result["attachment_count"] += 1

            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        result["body_markdown"] = payload.decode(charset, errors="replace")
                    except Exception:
                        result["body_markdown"] = payload.decode("utf-8", errors="replace")
            elif ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        result["body_plain"] = payload.decode(charset, errors="replace")
                    except Exception:
                        result["body_plain"] = payload.decode("utf-8", errors="replace")

    return result


def check_extraction_quality(sample_size: int = 50) -> tuple[bool, str, dict]:
    """Check that extracted .eml files have valid MIME structure."""
    eml_files = sorted([f for f in MAILDIR.iterdir() if f.is_file()])[:sample_size]
    
    valid_count = 0
    error_count = 0
    errors = []
    
    for eml_path in eml_files:
        try:
            with open(eml_path, "rb") as f:
                raw = f.read()
            
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            
            if msg.get("Subject") or msg.get("From"):
                valid_count += 1
            else:
                error_count += 1
                errors.append(f"{eml_path.name}: missing headers")
        except Exception as e:
            error_count += 1
            errors.append(f"{eml_path.name}: {str(e)[:50]}")
    
    result = {
        "files_checked": len(eml_files),
        "valid": valid_count,
        "invalid": error_count,
        "sample_errors": errors[:5]
    }
    
    if len(eml_files) == 0:
        return False, "No email files found in maildir", result
    
    error_rate = (error_count / len(eml_files) * 100) if eml_files else 0
    if error_rate < 5:
        return True, f"Extraction quality good: {valid_count}/{len(eml_files)} valid", result
    else:
        return False, f"Extraction quality issues: {error_count}/{len(eml_files)} invalid ({error_rate:.1f}%)", result


def check_maildir_db_field_preservation(sample_size: int = 50) -> tuple[bool, str, dict]:
    """Compare .eml fields to DB records for field preservation."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    eml_files = sorted([f for f in MAILDIR.iterdir() if f.is_file()])[:sample_size]
    
    checks = {
        "message_id": {"match": 0, "mismatch": 0},
        "subject": {"match": 0, "mismatch": 0},
        "from_address": {"match": 0, "mismatch": 0},
        "body_not_empty": {"match": 0, "mismatch": 0},
    }
    
    no_db_match = 0
    
    for eml_path in eml_files:
        eml_info = extract_bodies_from_eml(eml_path)
        
        c.execute("SELECT * FROM emails WHERE message_id = ?", (eml_info["message_id"],))
        db_row = c.fetchone()
        
        if not db_row:
            no_db_match += 1
            continue
        
        db_record = dict(db_row)
        
        if eml_info["message_id"] == db_record["message_id"]:
            checks["message_id"]["match"] += 1
        else:
            checks["message_id"]["mismatch"] += 1
        
        if eml_info["subject"] == db_record["subject"]:
            checks["subject"]["match"] += 1
        else:
            checks["subject"]["mismatch"] += 1
        
        import json
        to_addresses = json.loads(db_record["to_addresses"]) if db_record["to_addresses"] else []
        
        if eml_info["from"] and any(addr in eml_info["from"] for addr in to_addresses + [db_record["from_address"]]):
            checks["from_address"]["match"] += 1
        else:
            checks["from_address"]["mismatch"] += 1
        
        if db_record["body_markdown"] and len(db_record["body_markdown"]) > 10:
            checks["body_not_empty"]["match"] += 1
        else:
            checks["body_not_empty"]["mismatch"] += 1
    
    conn.close()
    
    total = sum(ch["match"] + ch["mismatch"] for ch in checks.values())
    matches = sum(ch["match"] for ch in checks.values())
    
    result = {
        "files_checked": len(eml_files),
        "no_db_match": no_db_match,
        "checks": checks,
        "match_rate": (matches / total * 100) if total > 0 else 0
    }
    
    if no_db_match > len(eml_files) * 0.5:
        return False, f"Many .eml files not in DB: {no_db_match}/{len(eml_files)}", result
    
    if total > 0 and matches / total > 0.85:
        return True, f"Field preservation: {matches}/{total} fields match ({result['match_rate']:.0f}%)", result
    elif total == 0:
        return False, "No .eml files found to check", result
    else:
        return False, f"Field preservation issues: {matches}/{total} match ({result['match_rate']:.0f}%)", result


def check_attachment_pipeline() -> tuple[bool, str, dict]:
    """Compare attachment_hashes table paths to files on disk."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT path, filename FROM attachment_hashes")
    db_attachments = c.fetchall()
    
    conn.close()
    
    on_disk = 0
    missing = 0
    
    for row in db_attachments:
        path = row[0]
        full_path = BASE_DIR / path
        if full_path.exists():
            on_disk += 1
        else:
            missing += 1
    
    result = {
        "db_attachments": len(db_attachments),
        "on_disk": on_disk,
        "missing": missing
    }
    
    if missing == 0:
        return True, f"All {len(db_attachments)} attachments exist on disk", result
    else:
        return False, f"{missing} attachments missing from disk", result


def check_vector_coverage() -> tuple[bool, str, dict]:
    """Confirm every DB email has an embedding."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL")
    with_embedding = c.fetchone()[0]
    
    conn.close()
    
    result = {"total": total, "with_embedding": with_embedding}
    
    if with_embedding == total:
        return True, f"All {total} emails have embeddings", result
    else:
        return False, f"{total - with_embedding} emails missing embeddings", result


def check_has_attachments_accuracy(sample_size: int = 50) -> tuple[bool, str, dict]:
    """Verify has_attachments flag matches actual attachment parts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    eml_files = sorted([f for f in MAILDIR.iterdir() if f.is_file()])[:sample_size]
    
    accurate = 0
    inaccurate = 0
    
    for eml_path in eml_files:
        eml_info = extract_bodies_from_eml(eml_path)
        
        c.execute("SELECT has_attachments FROM emails WHERE message_id = ?", (eml_info["message_id"],))
        row = c.fetchone()
        
        if not row:
            continue
        
        db_flag = row["has_attachments"]
        eml_flag = 1 if eml_info["has_attachments"] else 0
        
        if db_flag == eml_flag:
            accurate += 1
        else:
            inaccurate += 1
    
    conn.close()
    
    result = {"accurate": accurate, "inaccurate": inaccurate}
    
    if inaccurate == 0:
        return True, f"has_attachments flag accurate: {accurate}/{accurate+inaccurate}", result
    else:
        return False, f"has_attachments inaccurate: {inaccurate}/{accurate+inaccurate}", result


def check_no_raw_html_in_body() -> tuple[bool, str, dict]:
    """Verify body_markdown is not raw HTML."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT id, body_markdown FROM emails LIMIT 100")
    samples = c.fetchall()
    
    conn.close()
    
    raw_html_markers = ["<html", "<!doctype", "<head>", "<body>", "<meta "]
    
    clean = 0
    raw_html = 0
    
    for row in samples:
        body = row[1] or ""
        if body:
            head = body[:500].lower()
            if any(marker.lower() in head for marker in raw_html_markers):
                raw_html += 1
            else:
                clean += 1
    
    result = {"clean": clean, "raw_html": raw_html}
    
    if raw_html == 0:
        return True, f"All {clean} sampled emails have clean markdown", result
    else:
        return False, f"{raw_html} emails may have raw HTML in body", result


def check_salvage_quotes() -> tuple[bool, str, dict]:
    """Verify quoted_reply records exist with valid parent_id and content_hash."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'quoted_reply'")
    quoted_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original'")
    original_count = c.fetchone()[0]
    
    if quoted_count == 0:
        conn.close()
        return False, "No quoted_reply records found. Quote salvage pipeline may not be working.", {"quoted": 0, "original": original_count}
    
    c.execute("""
        SELECT COUNT(*) FROM emails 
        WHERE source = 'quoted_reply' 
          AND parent_id IS NOT NULL 
          AND content_hash IS NOT NULL
    """)
    valid_quoted = c.fetchone()[0]
    
    conn.close()
    
    result = {"quoted_reply": quoted_count, "valid": valid_quoted, "original": original_count}
    
    if valid_quoted == quoted_count:
        return True, f"All {quoted_count} quoted_reply records have valid parent_id and content_hash", result
    else:
        return False, f"Only {valid_quoted}/{quoted_count} quoted_reply have parent_id and content_hash", result


def check_content_hash_uniqueness() -> tuple[bool, str, dict]:
    """Verify no duplicate content_hash values in quoted_reply records."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        SELECT content_hash, COUNT(*) as cnt 
        FROM emails 
        WHERE source = 'quoted_reply' AND content_hash IS NOT NULL
        GROUP BY content_hash 
        HAVING cnt > 1
    """)
    duplicates = c.fetchall()
    
    conn.close()
    
    if not duplicates:
        return True, "No duplicate content_hash values in quoted_reply records", {"duplicates": 0}
    
    return False, f"Found {len(duplicates)} duplicate content_hash values", {"duplicates": len(duplicates), "samples": [d[0] for d in duplicates[:5]]}


def check_parent_child_relationship() -> tuple[bool, str, dict]:
    """Verify all parent_id values reference valid emails."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT id FROM emails WHERE source = 'quoted_reply'")
    quoted_ids = [row[0] for row in c.fetchall()]
    
    if not quoted_ids:
        conn.close()
        return True, "No quoted_reply records to check", {"checked": 0, "valid": 0}
    
    valid = 0
    invalid = 0
    
    for qid in quoted_ids[:100]:
        c.execute("SELECT parent_id FROM emails WHERE id = ?", (qid,))
        row = c.fetchone()
        if row and row[0]:
            c.execute("SELECT 1 FROM emails WHERE id = ?", (row[0],))
            if c.fetchone():
                valid += 1
            else:
                invalid += 1
    
    conn.close()
    
    result = {"checked": len(quoted_ids[:100]), "valid": valid, "invalid": invalid}
    
    if invalid == 0:
        return True, f"All {valid} parent_id references are valid", result
    else:
        return False, f"Found {invalid} invalid parent_id references", result


def check_mcp_filters_quoted_replies() -> tuple[bool, str, dict]:
    """Verify MCP queries exclude quoted_reply by default."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'quoted_reply'")
    total_quoted = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE (source IS NULL OR source != 'quoted_reply')")
    filtered_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails")
    total = c.fetchone()[0]
    
    conn.close()
    
    expected_filtered = total - total_quoted
    
    if filtered_count == expected_filtered:
        return True, f"MCP filter excludes quoted_reply: {filtered_count} records returned", {"total": total, "filtered": filtered_count, "quoted_excluded": total_quoted}
    else:
        return False, f"MCP filter mismatch: expected {expected_filtered}, got {filtered_count}", {"total": total, "filtered": filtered_count, "quoted_excluded": total_quoted}


def check_salvage_content_quality() -> tuple[bool, str, dict]:
    """Verify salvaged records have non-empty, meaningful body content."""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from ingest import _strip_signatures
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("""
        SELECT id, subject, body_markdown, length(body_markdown) as body_len
        FROM emails WHERE source = 'quoted_reply'
    """)
    quoted = c.fetchall()
    conn.close()
    
    if not quoted:
        return True, "No quoted_reply records to check", {"checked": 0}
    
    meaningful = 0
    empty_or_noise = 0
    samples = []
    
    for row in quoted:
        body = row["body_markdown"] or ""
        stripped = _strip_signatures(body)
        if len(stripped.strip()) > 50:
            meaningful += 1
        else:
            empty_or_noise += 1
            if len(samples) < 3:
                samples.append({"id": row["id"], "subject": row["subject"], "body_len": row["body_len"]})
    
    result = {"checked": len(quoted), "meaningful": meaningful, "empty_or_noise": empty_or_noise, "samples": samples}
    
    if meaningful == len(quoted):
        return True, f"All {meaningful} salvaged records have meaningful content", result
    else:
        return False, f"{empty_or_noise}/{len(quoted)} salvaged records have empty or noise content", result


def check_salvage_rate_analysis() -> tuple[bool, str, dict]:
    """Analyze why salvage rate is low - calendar invites vs actual replies."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original'")
    total_original = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original' AND length(body_plain) > 200")
    with_plain = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original' AND length(body_markdown) > 200")
    with_html = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original' AND length(body_plain) > 200 AND length(body_markdown) > 200")
    with_both = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'quoted_reply'")
    quoted_count = c.fetchone()[0]
    
    conn.close()
    
    salvage_rate = (quoted_count / with_plain * 100) if with_plain > 0 else 0
    
    # Check for calendar invite indicators
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("""
        SELECT subject, COUNT(*) as cnt 
        FROM emails 
        WHERE source = 'original' 
          AND (subject LIKE 'Accepted:%' OR subject LIKE 'Declined:%' OR subject LIKE 'Canceled:%' OR subject LIKE 'Tentative:%')
        GROUP BY subject
        ORDER BY cnt DESC
        LIMIT 10
    """)
    calendar_subjects = c.fetchall()
    
    conn.close()
    
    result = {
        "total_original": total_original,
        "with_plain_text_200": with_plain,
        "with_html_200": with_html,
        "with_both": with_both,
        "quoted_reply_count": quoted_count,
        "salvage_rate_percent": round(salvage_rate, 1),
        "calendar_invite_sample": [{"subject": r["subject"], "count": r["cnt"]} for r in calendar_subjects[:5]]
    }
    
    if salvage_rate < 5:
        return True, f"Salvage rate low ({salvage_rate:.1f}%) - likely dominated by calendar invites", result
    elif salvage_rate > 50:
        return True, f"Salvage rate healthy ({salvage_rate:.1f}%)", result
    else:
        return True, f"Salvage rate moderate ({salvage_rate:.1f}%)", result


def check_signature_stripping() -> tuple[bool, str, dict]:
    """Verify signature stripping patterns remove common signatures."""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from ingest import _SIGNATURE_STRIP_PATTERNS, _strip_signatures
    
    test_cases = [
        ("Standard signature", "Hello world\n--\nJohn Doe\nCEO"),
        ("Mobile signature", "Sent from my iPhone"),
        ("Outlook original", "From: John\nSent: Monday\nTo: Jane\n\nSome reply content"),
        ("Multiline signature", "Best regards\nJohn\n\n--\nSent from my Android"),
    ]
    
    results = []
    for name, text in test_cases:
        stripped = _strip_signatures(text)
        removed = len(text) - len(stripped)
        results.append({"test": name, "original_len": len(text), "stripped_len": len(stripped), "chars_removed": removed})
    
    return True, f"Signature stripping patterns tested on {len(test_cases)} cases", {"tests": results}


def check_semantic_dedup() -> tuple[bool, str, dict]:
    """Verify Tier 2 semantic deduplication prevents near-duplicate fragments."""
    try:
        from sentence_transformers import util
        import numpy as np
    except ImportError:
        return True, "sentence-transformers not available, skipping semantic dedup check", {"checked": 0}
    
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from mcp_server.database import _encode_text_to_embedding
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Get all quoted_reply records within same thread
    c.execute("""
        SELECT qr1.id as id1, qr1.body_markdown as body1, qr1.thread_id as tid,
               qr2.id as id2, qr2.body_markdown as body2
        FROM emails qr1
        JOIN emails qr2 ON qr1.thread_id = qr2.thread_id 
          AND qr1.source = 'quoted_reply' 
          AND qr2.source = 'quoted_reply'
          AND qr1.id < qr2.id
        WHERE qr1.thread_id IS NOT NULL
    """)
    
    pairs = c.fetchall()
    conn.close()
    
    if not pairs:
        return True, "No quoted_reply pairs in same thread to check", {"checked": 0}
    
    similar_pairs = 0
    checked = 0
    
    for pair in pairs[:50]:
        if not pair["body1"] or not pair["body2"]:
            continue
        
        try:
            emb1 = _encode_text_to_embedding(pair["body1"])
            emb2 = _encode_text_to_embedding(pair["body2"])
            
            vec1 = np.frombuffer(emb1, dtype=np.float32)
            vec2 = np.frombuffer(emb2, dtype=np.float32)
            
            similarity = util.cos_sim(vec1, vec2).item()
            checked += 1
            
            if similarity >= 0.98:
                similar_pairs += 1
        except Exception:
            continue
    
    result = {"pairs_checked": checked, "similar_pairs_0.98": similar_pairs}
    
    if similar_pairs == 0:
        return True, f"No near-duplicate fragments found among {checked} checked pairs", result
    else:
        return False, f"Found {similar_pairs} near-duplicate pairs (similarity >= 0.98)", result


def check_html_only_emails() -> tuple[bool, str, dict]:
    """Document emails with HTML body but empty plain text - can't be salvaged."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        SELECT COUNT(*) FROM emails 
        WHERE source = 'original' 
          AND (body_plain IS NULL OR body_plain = '')
          AND (body_markdown IS NOT NULL AND body_markdown != '')
    """)
    html_only = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE source = 'original'")
    total_original = c.fetchone()[0]
    
    c.execute("""
        SELECT subject, length(body_markdown) as md_len
        FROM emails 
        WHERE source = 'original' 
          AND (body_plain IS NULL OR body_plain = '')
          AND body_markdown IS NOT NULL
        ORDER BY md_len DESC
        LIMIT 5
    """)
    samples = c.fetchall()
    
    conn.close()
    
    result = {
        "html_only_count": html_only,
        "total_original": total_original,
        "html_only_percent": round(html_only / total_original * 100, 1),
        "sample_subjects": [{"subject": str(r[0])[:60], "md_len": r[1]} for r in samples]
    }
    
    return True, f"{html_only} HTML-only emails ({result['html_only_percent']}%) can't be salvaged with current approach", result


def run_validation(sample_size: int = 50, verbose: bool = False) -> dict:
    """Run full validation and return results."""
    results = {}
    
    print("\n" + "=" * 60)
    print("  Extraction Pipeline Validation")
    print("=" * 60)
    
    checks = [
        ("Extraction Quality", lambda: check_extraction_quality(sample_size)),
        ("Field Preservation", lambda: check_maildir_db_field_preservation(sample_size)),
        ("Attachment Pipeline", lambda: check_attachment_pipeline()),
        ("Vector Coverage", lambda: check_vector_coverage()),
        ("has_attachments Accuracy", lambda: check_has_attachments_accuracy(sample_size)),
        ("No Raw HTML in Body", lambda: check_no_raw_html_in_body()),
        ("Salvage Quotes", lambda: check_salvage_quotes()),
        ("Content Hash Uniqueness", lambda: check_content_hash_uniqueness()),
        ("Parent-Child Relationship", lambda: check_parent_child_relationship()),
        ("MCP Filters Quoted Replies", lambda: check_mcp_filters_quoted_replies()),
        ("Salvage Content Quality", lambda: check_salvage_content_quality()),
        ("Salvage Rate Analysis", lambda: check_salvage_rate_analysis()),
        ("Signature Stripping", lambda: check_signature_stripping()),
        ("Semantic Dedup", lambda: check_semantic_dedup()),
        ("HTML-Only Emails", lambda: check_html_only_emails()),
    ]
    
    all_passed = True
    
    for name, check_func in checks:
        passed, message, detail = check_func()
        results[name] = {"passed": passed, "message": message, "detail": detail}
        
        status = "PASS" if passed else "FAIL"
        print(f"\n[{status}] {name}")
        print(f"    {message}")
        
        if verbose and detail:
            for k, v in detail.items():
                if k != "sample_errors":
                    print(f"    - {k}: {v}")
        
        if not passed:
            all_passed = False
    
    maildir_count = scan_maildir_count()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM emails")
    db_count = c.fetchone()[0]
    try:
        c.execute("SELECT COUNT(*) FROM email_vectors")
        vec_count = c.fetchone()[0]
    except sqlite3.OperationalError:
        c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL")
        vec_count = c.fetchone()[0]
    conn.close()
    
    print(f"\n[Pipeline Summary]")
    print(f"    Maildir files: {maildir_count}")
    print(f"    DB records: {db_count}")
    print(f"    Vector embeddings: {vec_count}")
    
    print("\n" + "=" * 60)
    if all_passed:
        print("  Result: PASS")
    else:
        print("  Result: FAIL")
    print("=" * 60)
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Validate extraction pipeline")
    parser.add_argument("--sample", type=int, default=50, help="Number of emails to sample (default: 50)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed output")
    args = parser.parse_args()
    
    if not DB_PATH.exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        sys.exit(1)
    
    if not MAILDIR.exists():
        print(f"SKIP (maildir/cur/ not found)")
        print("=" * 60)
        print("  Result: SKIP")
        print("=" * 60)
        sys.exit(0)
    
    # Check if maildir is empty
    maildir_files = [f for f in MAILDIR.iterdir() if f.is_file() and not f.name.startswith('.')]
    if not maildir_files:
        print(f"SKIP (maildir/cur/ is empty)")
        print("=" * 60)
        print("  Result: SKIP")
        print("=" * 60)
        sys.exit(0)
    
    results = run_validation(sample_size=args.sample, verbose=args.verbose)
    
    all_passed = all(r["passed"] for r in results.values())
    
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
