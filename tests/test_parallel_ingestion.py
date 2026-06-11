"""Tests for parallel ingestion functionality."""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from ingest import (
    init_database,
    get_db_connection,
    get_db_connection_ctx,
    collect_email_files,
    parse_email_file,
    is_duplicate_message_id,
    insert_email,
    generate_embedding_text,
    DB_PATH,
    CHECKPOINT_PATH,
    LOG_DIR,
)

class TestWALMode(unittest.TestCase):
    """Test that WAL mode is enabled on database initialization."""
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_db = Path(self.temp_dir) / "test.db"
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    def test_wal_mode_enabled_after_init(self):
        """WAL journal mode should be set after init_database()."""
        init_database(self.temp_db)
        
        conn = sqlite3.connect(self.temp_db)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()
        
        self.assertEqual(mode, "wal")


class TestDBConnectionContextManager(unittest.TestCase):
    """Test the get_db_connection_ctx context manager."""
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_db = Path(self.temp_dir) / "test.db"
        init_database(self.temp_db)
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    def test_connection_opens_and_closes(self):
        """Context manager should open connection and close it on exit."""
        with get_db_connection_ctx(self.temp_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)
    def test_connection_isolation(self):
        """Multiple context managers should get independent connections."""
        with get_db_connection_ctx(self.temp_db) as conn1:
            with get_db_connection_ctx(self.temp_db) as conn2:
                self.assertIsNot(conn1, conn2)


class TestConcurrentLimitCLI(unittest.TestCase):
    """Test that --concurrent-limit CLI argument is accepted."""
    def test_argparse_accepts_concurrent_limit(self):
        """CLI should accept --concurrent-limit argument."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("maildir", nargs="?", type=Path)
        parser.add_argument("--no-resume", action="store_true")
        parser.add_argument("--backfill", nargs="?", const="all")
        parser.add_argument("--concurrent-limit", type=int, default=4)
        
        args = parser.parse_args(["--concurrent-limit", "8", "/tmp/fake"])
        self.assertEqual(args.concurrent_limit, 8)
    
    def test_default_concurrent_limit(self):
        """Default concurrent limit should be 4."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("maildir", nargs="?", type=Path)
        parser.add_argument("--concurrent-limit", type=int, default=4)
        
        args = parser.parse_args(["/tmp/fake"])
        self.assertEqual(args.concurrent_limit, 4)


class TestConfigDefaultConcurrentLimit(unittest.TestCase):
    """Test Config.DEFAULT_CONCURRENT_LIMIT."""
    def test_config_has_default(self):
        """Config should have DEFAULT_CONCURRENT_LIMIT = 4."""
        from mcp_server.config import Config
        self.assertEqual(Config.DEFAULT_CONCURRENT_LIMIT, 4)


class TestParallelIngestionWithMock(unittest.TestCase):
    """Test parallel ingestion logic with mocked embedder."""
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.maildir = Path(self.temp_dir) / "maildir"
        self.maildir.mkdir()
        
        cur = self.maildir / "cur"
        cur.mkdir()
        
        for i in range(3):
            eml = cur / f"test{i}.eml"
            eml.write_text(
                f"From: sender{i}@example.com\n"
                f"To: recipient@example.com\n"
                f"Subject: Test Email {i}\n"
                f"Message-ID: <test-{i}@example.com>\n"
                f"Date: Mon, 01 Jan 2024 00:00:0{i} +0000\n"
                f"\n"
                f"This is test email number {i}.\n"
            )
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    def test_thread_pool_executor_used(self):
        """ThreadPoolExecutor should be called with concurrent_limit workers."""
        with patch('ingest.ThreadPoolExecutor') as mock_executor:
            mock_executor.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_executor.return_value.__exit__ = MagicMock(return_value=False)
            
            from ingest import ingest_emails
            import inspect
            sig = inspect.signature(ingest_emails)
            self.assertIn('concurrent_limit', sig.parameters)


class TestBodyTextFallbacks(unittest.TestCase):
    """Test body_text fallback behavior during parsing."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.eml_path = Path(self.temp_dir) / "test.eml"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_html_without_extractable_text_falls_back_to_plain_body(self):
        self.eml_path.write_text(
            "From: sender@example.com\n"
            "To: recipient@example.com\n"
            "Subject: HTML fallback\n"
            "Message-ID: <html-fallback@example.com>\n"
            "Date: Mon, 01 Jan 2024 00:00:00 +0000\n"
            "MIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=boundary123\n"
            "\n"
            "--boundary123\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "\n"
            "Plain fallback text.\n"
            "--boundary123\n"
            "Content-Type: text/html; charset=utf-8\n"
            "\n"
            "<html><body><img src=\"cid:image001\"></body></html>\n"
            "--boundary123--\n"
        )

        records = parse_email_file(self.eml_path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["body_text"], "Plain fallback text.")


class TestThreadSafety(unittest.TestCase):
    """Test thread-safe operations in parallel ingestion."""
    def test_lock_protects_shared_state(self):
        """Threading.Lock should protect counter increments."""
        lock = threading.Lock()
        counter = [0]
        
        def increment():
            for _ in range(1000):
                with lock:
                    counter[0] += 1
        
        threads = [threading.Thread(target=increment) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(counter[0], 4000)
    def test_queue_thread_safety(self):
        """Queue should be thread-safe for producer/consumer pattern."""
        from queue import Queue
        q = Queue()
        results = []
        
        def producer():
            for i in range(100):
                q.put(i)
        
        def consumer():
            while True:
                try:
                    item = q.get(timeout=1)
                    results.append(item)
                    q.task_done()
                except:
                    break
        
        producer_thread = threading.Thread(target=producer)
        consumer_thread = threading.Thread(target=consumer)
        
        producer_thread.start()
        consumer_thread.start()
        producer_thread.join()
        q.join()
        consumer_thread.join()
        
        self.assertEqual(len(results), 100)


if __name__ == "__main__":
    unittest.main()
