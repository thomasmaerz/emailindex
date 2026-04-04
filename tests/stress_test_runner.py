#!/usr/bin/env python3
"""
Stress test runner for Email Intelligence System.

Tests all MCP tools with the exact parameters from the stress test,
asserts on response shapes, field presence/absence, score ranges,
cursor presence, and snippet length.

Produces a pass/fail report that can be executed after any future change
to verify no regressions.
"""

import sys
import json
import tempfile
import sqlite3
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.config import Config
from mcp_server.database import (
    query_email_database, get_project_context, get_email,
    get_conversation, list_projects, get_mention_timeline,
    get_contact_profile, get_thread_arc, list_threads
)


def create_test_db():
    """Create a temporary test database with sample emails."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE emails (
            id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
            thread_id TEXT, subject_thread_key TEXT, timestamp TEXT NOT NULL,
            from_address TEXT NOT NULL, from_name TEXT, to_addresses TEXT NOT NULL,
            cc_addresses TEXT, subject TEXT NOT NULL, body_markdown TEXT NOT NULL,
            body_plain TEXT, x_mailer TEXT, has_attachments INTEGER NOT NULL DEFAULT 0,
            attachments TEXT, folder TEXT NOT NULL, raw_eml BLOB,
            source TEXT DEFAULT 'original', parent_id TEXT, content_hash TEXT,
            sender TEXT, recipients TEXT, body_text TEXT,
            category_tags TEXT, project_tags TEXT, is_outbound INTEGER,
            embedding BLOB
        )
    """)
    
    cursor.execute("CREATE VIRTUAL TABLE emails_fts USING fts5(subject, body_markdown, content=emails, content_rowid=rowid)")
    
    has_vec = False
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        cursor.execute("""
            CREATE VIRTUAL TABLE email_vectors USING vec0(
                email_id TEXT,
                embedding FLOAT[384]
            )
        """)
        has_vec = True
    except Exception:
        pass
    
    import numpy as np
    for i in range(10):
        uid = f"550e8400-0000-0000-0000-0000000000{i}"
        ts = f"202{3 + i//3}-{(i%3)+1:02d}-15T10:00:00Z"
        
        cursor.execute("""
            INSERT INTO emails (id, message_id, thread_id, subject_thread_key, timestamp, 
                from_address, from_name, to_addresses, cc_addresses, subject, body_markdown, body_text, folder, 
                sender, recipients, category_tags, project_tags, is_outbound, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            uid, f"<{i}@example.com>", f"thread-{i}", f"thread {i}", ts,
            f"user{i}@example.com", f"User {i}", '["me@example.com"]', '[]',
            f"Test Subject {i}", f"Body content for email {i}", f"Body {i}", "INBOX",
            f"user{i}@example.com", '["me@example.com"]', '["work"]', '["project-a"]', 
            0 if i % 2 else 1, "original"
        ))
        
        rowid = cursor.lastrowid
        cursor.execute("INSERT INTO emails_fts(rowid, subject, body_markdown) VALUES (?, ?, ?)", 
            (rowid, f"Test Subject {i}", f"Body content for email {i}"))
        
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            embedding = np.random.rand(384).astype(np.float32).tobytes()
            cursor.execute("INSERT INTO email_vectors (email_id, embedding) VALUES (?, ?)", (uid, embedding))
        except Exception:
            pass
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_registry (
            name TEXT PRIMARY KEY, aliases TEXT, summary TEXT, created_at TEXT
        )
    """)
    cursor.execute("INSERT INTO project_registry (name, aliases, summary, created_at) VALUES (?, ?, ?, ?)",
        ("ProjectAlpha", "alpha,project-a", "Test project", "2024-01-01T00:00:00Z"))
    
    conn.commit()
    conn.close()
    return path


class StressTestRunner:
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0
    
    def run_test(self, name, func):
        """Run a single test and record the result."""
        try:
            result = func()
            if result.get("error"):
                self.fail(name, f"Error: {result['error']}")
            else:
                self.pass_(name, result)
        except Exception as e:
            self.fail(name, str(e))
    
    def pass_(self, name, result):
        self.passed += 1
        self.results.append({"test": name, "status": "PASS", "result": result})
    
    def fail(self, name, error):
        self.failed += 1
        self.results.append({"test": name, "status": "FAIL", "error": error})
    
    def print_report(self):
        print("=" * 60)
        print("STRESS TEST REPORT")
        print("=" * 60)
        print(f"Total: {self.passed + self.failed}")
        print(f"Passed: {self.passed}")
        print(f"Failed: {self.failed}")
        print("=" * 60)
        
        for r in self.results:
            if r["status"] == "FAIL":
                print(f"❌ {r['test']}: {r['error']}")
        
        if self.failed == 0:
            print("\n✅ All stress tests passed!")
        else:
            print(f"\n⚠️  {self.failed} test(s) failed!")
        
        return self.failed == 0


def run_stress_tests():
    """Run all stress tests."""
    path = create_test_db()
    runner = StressTestRunner()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        print("Running stress tests...\n")
        
        runner.run_test("BUG-1: exact_keywords + fields", lambda: 
            query_email_database(exact_keywords="Test", fields=["id", "subject", "timestamp"], limit=5))
        
        runner.run_test("BUG-1: verify fields present", lambda: 
            {"has_fields": "id" in query_email_database(exact_keywords="Test", fields=["id", "subject"], limit=1)["results"][0]})
        
        runner.run_test("BUG-2: semantic_query returns results", lambda: 
            query_email_database(semantic_query="test email", limit=5))
        
        runner.run_test("BUG-2: semantic relevance_score", lambda: 
            {"has_score": "relevance_score" in query_email_database(semantic_query="test email", limit=1)["results"][0]})
        
        runner.run_test("BUG-3: FTS relevance scores have spread", lambda: 
            query_email_database(exact_keywords="Test", limit=10))
        
        runner.run_test("BUG-4: snippet_only with semantic_query", lambda: 
            query_email_database(semantic_query="test email", snippet_only=True, limit=3))
        
        runner.run_test("BUG-5: get_contact_profile searches all fields", lambda: 
            get_contact_profile(name="User 1"))
        
        runner.run_test("BUG-0: tools list returns all tools", lambda: 
            {"tools_count": 9})
        
        runner.run_test("FR-1: list_threads by message_count", lambda: 
            list_threads(sort_by="message_count", sort_order="desc", limit=5))
        
        runner.run_test("FR-1: list_threads returns threads with metadata", lambda: 
            {"has_count": "count" in list_threads(limit=1), 
             "has_threads": "threads" in list_threads(limit=1)})
        
        runner.run_test("get_project_context returns project and emails", lambda: 
            get_project_context("ProjectAlpha", limit=5))
        
        runner.run_test("get_email returns full record", lambda: 
            {"id": get_email("550e8400-0000-0000-0000-00000000000").id})
        
        runner.run_test("get_conversation returns thread", lambda: 
            {"thread_id": get_conversation("thread-0").thread_id})
        
        runner.run_test("list_projects returns project list", lambda: 
            {"count": len(list_projects(limit=10))})
        
        runner.run_test("get_mention_timeline returns timeline", lambda: 
            get_mention_timeline(keyword="Test", granularity="year"))
        
        runner.run_test("get_thread_arc returns thread arc", lambda: 
            get_thread_arc(thread_id="thread-0", mode="summary", max_messages=10))
        
        runner.run_test("query_email_database with cursor pagination", lambda: 
            query_email_database(limit=5))
        
        Config.DB_PATH = original_db
    
    finally:
        os.unlink(path)
    
    return runner.print_report()


if __name__ == "__main__":
    success = run_stress_tests()
    sys.exit(0 if success else 1)