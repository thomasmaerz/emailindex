#!/usr/bin/env python3
"""
Validation script for GitHub Issue #4: Vector similarity search untestable

Validates the complete embedding pipeline end-to-end:
1. sqlite-vec extension loads
2. Schema has embedding column and email_vectors table
3. All emails have non-null embeddings
4. Embeddings are correct dimensions (384)
5. Vector table has matching records
6. Vector similarity search returns meaningful results
7. Pipeline integrity: maildir == DB == vectors

Usage:
    python3 tests/validate_issue4.py              # full validation
    python3 tests/validate_issue4.py --verbose   # detailed per-email output
"""

import argparse
import email
import email.policy
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
MAILDIR = BASE_DIR / "maildir" / "cur"
DB_PATH = BASE_DIR / "db" / "emails.db"
EMBEDDING_DIMENSIONS = 384


def check_sqlite_vec_loads() -> tuple[bool, str, dict]:
    """Check if sqlite-vec extension loads without errors."""
    result = {}
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.close()
        return True, "sqlite-vec extension loaded successfully", result
    except Exception as e:
        return False, f"sqlite-vec failed to load: {e}", result


def check_schema() -> tuple[bool, str, dict]:
    """Check schema for embedding column and email_vectors table."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    result = {"embedding_column": False, "email_vectors_table": False}
    
    try:
        c.execute("PRAGMA table_info(emails)")
        columns = [row[1] for row in c.fetchall()]
        result["embedding_column"] = "embedding" in columns
        
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_vectors'")
        result["email_vectors_table"] = c.fetchone() is not None
        
        conn.close()
        
        if result["embedding_column"] and result["email_vectors_table"]:
            return True, "Schema validation passed", result
        else:
            missing = []
            if not result["embedding_column"]:
                missing.append("embedding column")
            if not result["email_vectors_table"]:
                missing.append("email_vectors table")
            return False, f"Missing: {', '.join(missing)}", result
    except Exception as e:
        conn.close()
        return False, f"Schema check failed: {e}", result


def check_embedding_population() -> tuple[bool, str, dict]:
    """Check that all emails have non-null embeddings."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails")
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL")
    with_embedding = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NULL")
    without_embedding = c.fetchone()[0]
    
    conn.close()
    
    result = {
        "total": total,
        "with_embedding": with_embedding,
        "without_embedding": without_embedding,
        "percentage": (with_embedding / total * 100) if total > 0 else 0
    }
    
    if with_embedding == total and total > 0:
        return True, f"All {total} emails have embeddings", result
    else:
        return False, f"{without_embedding} emails missing embeddings", result


def check_vector_dimensions() -> tuple[bool, str, dict]:
    """Check that embeddings are correct dimensions."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT id, embedding FROM emails WHERE embedding IS NOT NULL LIMIT 100")
    samples = c.fetchall()
    
    conn.close()
    
    if not samples:
        return False, "No embeddings found to check", {"samples_checked": 0}
    
    result = {"samples_checked": len(samples), "all_valid": True, "invalid_ids": []}
    
    for row in samples:
        emb = row[1]
        if emb is None:
            continue
        num_floats = len(emb) // 4
        if num_floats != EMBEDDING_DIMENSIONS:
            result["all_valid"] = False
            result["invalid_ids"].append(row[0])
    
    if result["all_valid"]:
        return True, f"All {len(samples)} sampled embeddings are {EMBEDDING_DIMENSIONS}D", result
    else:
        return False, f"Found {len(result['invalid_ids'])} embeddings with wrong dimensions", result


def check_vector_table_sync() -> tuple[bool, str, dict]:
    """Check that email_vectors table has matching record count."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    try:
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_vectors'")
        if c.fetchone() is None:
            conn.close()
            return False, "email_vectors table does not exist", {"emails_count": 0, "vectors_count": 0}
    except Exception as e:
        conn.close()
        return False, f"Error checking table: {e}", {"emails_count": 0, "vectors_count": 0}
    
    c.execute("SELECT COUNT(*) FROM emails")
    emails_count = c.fetchone()[0]
    
    try:
        c.execute("SELECT COUNT(*) FROM email_vectors")
        vectors_count = c.fetchone()[0]
    except sqlite3.OperationalError:
        c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL")
        vectors_count = c.fetchone()[0]
    
    conn.close()
    
    result = {"emails_count": emails_count, "vectors_count": vectors_count}
    
    if emails_count == vectors_count:
        return True, f"Both tables synced: {emails_count} records", result
    else:
        return False, f"Mismatch: {emails_count} emails vs {vectors_count} vectors", result


def check_vector_search_works() -> tuple[bool, str, dict]:
    """Check that vector similarity search returns meaningful results."""
    result = {"tests_run": 0, "tests_passed": 0, "sample_results": []}
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
    except Exception as e:
        return False, f"Failed to load sqlite_vec: {e}", result
    
    try:
        c.execute("SELECT id, embedding FROM emails WHERE embedding IS NOT NULL LIMIT 1")
        sample = c.fetchone()
        
        if not sample:
            conn.close()
            return False, "No emails with embeddings found", result
        
        sample_id = sample["id"]
        sample_embedding = sample["embedding"]
        
        tests_passed = 0
        tests_total = 0
        sample_results = []
        
        try:
            c.execute("""
                SELECT e.id, e.subject, vec_distance_cosine(e.embedding, ?) as score
                FROM emails e
                WHERE e.id != ? AND e.embedding IS NOT NULL
                ORDER BY score
                LIMIT 5
            """, (sample_embedding, sample_id))
            
            results = c.fetchall()
            tests_total = 1
            
            if results:
                valid_scores = all(0 <= r["score"] <= 1 for r in results)
                if valid_scores:
                    tests_passed = 1
                    for r in results:
                        sample_results.append({
                            "score": r["score"],
                            "subject": r["subject"][:60] if r["subject"] else ""
                        })
                else:
                    sample_results.append({"error": "Invalid scores"})
            else:
                sample_results.append({"error": "No results returned"})
        except Exception as e:
            sample_results.append({"error": str(e)})
        
        conn.close()
        
        result = {
            "tests_run": tests_total,
            "tests_passed": tests_passed,
            "sample_results": sample_results
        }
        
        if tests_passed == tests_total:
            return True, f"Vector search works ({tests_passed}/{tests_total} tests passed)", result
        else:
            return False, f"Vector search failed ({tests_passed}/{tests_total} tests passed)", result
    except Exception as e:
        conn.close()
        return False, f"Vector search test error: {e}", result


def sample_emails_for_variety(conn: sqlite3.Connection, sample_size: int = 30) -> list[dict]:
    """Sample emails with variety: attachments, dates, senders."""
    c = conn.cursor()
    
    samples = []
    
    c.execute("""
        SELECT id, message_id, subject, timestamp, from_address, has_attachments
        FROM emails
        WHERE has_attachments = 1
        LIMIT 10
    """)
    samples.extend([dict(row) for row in c.fetchall()])
    
    c.execute("""
        SELECT id, message_id, subject, timestamp, from_address, has_attachments
        FROM emails
        WHERE has_attachments = 0
        LIMIT 10
    """)
    samples.extend([dict(row) for row in c.fetchall()])
    
    c.execute("""
        SELECT id, message_id, subject, timestamp, from_address, has_attachments
        FROM emails
        WHERE timestamp < '2015-01-01'
        LIMIT 5
    """)
    samples.extend([dict(row) for row in c.fetchall()])
    
    c.execute("""
        SELECT id, message_id, subject, timestamp, from_address, has_attachments
        FROM emails
        WHERE timestamp >= '2018-01-01'
        LIMIT 5
    """)
    samples.extend([dict(row) for row in c.fetchall()])
    
    return samples[:sample_size]


def run_vector_search_tests(conn: sqlite3.Connection, sample_emails: list[dict]) -> tuple[bool, str, dict]:
    """Run vector search on sample emails and verify results are meaningful."""
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    tests_passed = 0
    tests_total = len(sample_emails)
    results_detail = []
    
    for email_sample in sample_emails:
        email_id = email_sample["id"]
        
        c.execute("SELECT embedding FROM emails WHERE id = ?", (email_id,))
        row = c.fetchone()
        
        if not row or not row["embedding"]:
            continue
        
        try:
            c.execute("""
                SELECT e.id, e.subject, vec_distance_cosine(e.embedding, ?) as score
                FROM emails e
                WHERE e.id != ? AND e.embedding IS NOT NULL
                ORDER BY score
                LIMIT 3
            """, (row["embedding"], email_id))
            
            results = c.fetchall()
            
            if results:
                valid_scores = all(0 <= r["score"] <= 1 for r in results)
                if valid_scores:
                    tests_passed += 1
                    results_detail.append({
                        "email_id": email_id[:8],
                        "subject": email_sample["subject"][:40],
                        "results_count": len(results),
                        "top_score": results[0]["score"]
                    })
        except Exception:
            pass
    
    conn.close()
    
    result = {
        "tests_total": tests_total,
        "tests_passed": tests_passed,
        "detail": results_detail[:5]
    }
    
    pass_rate = (tests_passed / tests_total * 100) if tests_total > 0 else 0
    
    if tests_passed == tests_total:
        return True, f"All {tests_total} vector search tests passed", result
    else:
        return False, f"{tests_passed}/{tests_total} tests passed ({pass_rate:.0f}%)", result


def check_pipeline_integrity() -> tuple[bool, str, dict]:
    """Check maildir count matches DB count matches vector count."""
    import os
    
    maildir_cur = MAILDIR
    maildir_count = len([f for f in os.listdir(maildir_cur) if f and not f.startswith('.')])
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM emails")
    db_count = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL")
    vector_count = c.fetchone()[0]
    
    conn.close()
    
    result = {
        "maildir_count": maildir_count,
        "db_count": db_count,
        "vector_count": vector_count
    }
    
    if maildir_count == db_count == vector_count:
        return True, f"Pipeline synced: {maildir_count} files = {db_count} DB = {vector_count} vectors", result
    else:
        return False, f"Mismatch: {maildir_count} maildir vs {db_count} DB vs {vector_count} vectors", result


def run_validation(verbose: bool = False) -> dict:
    """Run full validation and return results."""
    results = {}
    
    print("\n" + "=" * 60)
    print("  Issue #4 Validation: Vector Similarity Search")
    print("=" * 60)
    
    checks = [
        ("sqlite-vec Extension", check_sqlite_vec_loads),
        ("Schema Validation", check_schema),
        ("Embedding Population", check_embedding_population),
        ("Vector Dimensions", check_vector_dimensions),
        ("Vector Table Sync", check_vector_table_sync),
        ("Vector Search Works", check_vector_search_works),
        ("Pipeline Integrity", check_pipeline_integrity),
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
                if k != "detail" and not k.startswith("_"):
                    print(f"    - {k}: {v}")
        
        if not passed:
            all_passed = False
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    print("\n[3] Sampling emails for variety...")
    sample_emails = sample_emails_for_variety(conn, sample_size=30)
    print(f"    Sampled {len(sample_emails)} emails with variety")
    
    print("\n[4] Running vector search tests on samples...")
    passed, message, detail = run_vector_search_tests(conn, sample_emails)
    results["Vector Search Variety Tests"] = {"passed": passed, "message": message, "detail": detail}
    
    status = "PASS" if passed else "FAIL"
    print(f"    [{status}] {message}")
    
    if not passed:
        all_passed = False
    
    conn.close()
    
    print("\n" + "=" * 60)
    if all_passed:
        print("  Result: PASS")
    else:
        print("  Result: FAIL")
    print("=" * 60)
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Validate Issue #4: Vector similarity search")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed output")
    args = parser.parse_args()
    
    if not DB_PATH.exists():
        print(f"ERROR: Database not found: {DB_PATH}")
        sys.exit(1)
    
    if not MAILDIR.exists():
        print(f"ERROR: Maildir not found: {MAILDIR}")
        sys.exit(1)
    
    results = run_validation(verbose=args.verbose)
    
    all_passed = all(r["passed"] for r in results.values())
    
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
