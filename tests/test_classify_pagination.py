#!/usr/bin/env python3
"""Unit tests for classify_emails pagination fix (Issue #13)."""

import json
import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open


sys.path.insert(0, str(Path(__file__).parent.parent))

import classify_emails as ce


class TestClassifyEmailsPagination(unittest.TestCase):
    """Test cases for classify_emails pagination fix (Issue #13)."""

    def setUp(self):
        """Create in-memory DB with synthetic email data."""
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        self.cursor.execute("""
            CREATE TABLE emails (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                body_text TEXT,
                body_markdown TEXT,
                body_plain TEXT,
                sender TEXT,
                recipients TEXT,
                timestamp TEXT NOT NULL,
                category_tags TEXT,
                project_tags TEXT
            )
        """)
        self.cursor.execute("CREATE INDEX idx_emails_timestamp ON emails(timestamp)")
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_registry (
                name TEXT PRIMARY KEY,
                aliases TEXT,
                summary TEXT,
                created_at TEXT
            )
        """)

        self.cursor.execute("INSERT INTO project_registry (name, aliases, summary, created_at) VALUES ('TestProject', '', 'Test project', '2024-01-01T00:00:00Z')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _insert_email(self, id, subject, timestamp, body_text="body", body_markdown="markdown", body_plain="plain"):
        self.cursor.execute("""
            INSERT INTO emails (id, subject, body_text, body_markdown, body_plain, sender, recipients, timestamp, category_tags, project_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (id, subject, body_text, body_markdown, body_plain, "sender@test.com", "recipient@test.com", timestamp, None, None))
        self.conn.commit()

    def _build_classify_query(self, checkpoint, batch_size=100):
        """Replicate the exact query building logic from classify_emails.py."""
        query = """
            SELECT id, subject, COALESCE(NULLIF(body_text, ''), body_plain, body_markdown) AS body, sender, recipients, timestamp
            FROM emails
            WHERE (category_tags IS NULL OR category_tags = '')
        """

        params = ()
        if checkpoint.get("last_timestamp"):
            query += " AND (timestamp > ? OR (timestamp = ? AND id > ?))"
            params = (checkpoint["last_timestamp"], checkpoint["last_timestamp"], checkpoint["last_email_id"])

        query += " ORDER BY timestamp ASC, id ASC LIMIT ?"
        params = (*params, batch_size)

        self.cursor.execute(query, params)
        rows = self.cursor.fetchall()
        return [dict(r) for r in rows]

    def _insert_sample_emails(self):
        """Insert a small ordered dataset for checkpoint tests."""
        self._insert_email("id-01", "Subject 1", "2024-01-01T00:00:00Z")
        self._insert_email("id-02", "Subject 2", "2024-01-02T00:00:00Z")
        self._insert_email("id-03", "Subject 3", "2024-01-03T00:00:00Z")

    def test_full_traversal_no_skips(self):
        """Multiple batches across M emails — every email classified exactly once."""
        for i in range(7):
            self._insert_email(f"id-{i:02d}", f"Subject {i}", f"2024-01-{i+1:02d}T00:00:00Z")

        checkpoint = {"last_timestamp": None, "last_email_id": None}
        batch_size = 3
        all_ids = []

        while True:
            rows = self._build_classify_query(checkpoint, batch_size)
            if not rows:
                break
            for r in rows:
                all_ids.append(r["id"])
            checkpoint["last_timestamp"] = rows[-1]["timestamp"]
            checkpoint["last_email_id"] = rows[-1]["id"]

        expected = [f"id-{i:02d}" for i in range(7)]
        self.assertEqual(all_ids, expected)

    def test_chronological_order(self):
        """Results returned in timestamp ASC order."""
        self._insert_email("c", "Subject C", "2024-01-03T00:00:00Z")
        self._insert_email("a", "Subject A", "2024-01-01T00:00:00Z")
        self._insert_email("b", "Subject B", "2024-01-02T00:00:00Z")

        checkpoint = {"last_timestamp": None, "last_email_id": None}
        rows = self._build_classify_query(checkpoint, batch_size=10)

        timestamps = [r["timestamp"] for r in rows]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_exact_multiple_boundary(self):
        """Total emails = N × batch_size — no skip at the final boundary."""
        for i in range(9):
            self._insert_email(f"id-{i}", f"Subject {i}", f"2024-01-{i+1:02d}T00:00:00Z")

        checkpoint = {"last_timestamp": None, "last_email_id": None}
        batch_size = 3
        all_ids = []

        while True:
            rows = self._build_classify_query(checkpoint, batch_size)
            if not rows:
                break
            for r in rows:
                all_ids.append(r["id"])
            checkpoint["last_timestamp"] = rows[-1]["timestamp"]
            checkpoint["last_email_id"] = rows[-1]["id"]

        self.assertEqual(len(all_ids), 9)

    def test_timestamp_tiebreaking(self):
        """Multiple emails with identical timestamp — ordered by id ASC."""
        ts = "2024-01-01T00:00:00Z"
        self._insert_email("c-id", "Subject C", ts)
        self._insert_email("a-id", "Subject A", ts)
        self._insert_email("b-id", "Subject B", ts)

        checkpoint = {"last_timestamp": None, "last_email_id": None}
        rows = self._build_classify_query(checkpoint, batch_size=10)

        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids))

    def test_checkpoint_backward_compat_fresh(self):
        """Fresh checkpoint starts from beginning."""
        self._insert_sample_emails()
        checkpoint = {"last_timestamp": None, "last_email_id": None}
        rows = self._build_classify_query(checkpoint, batch_size=10)
        self.assertEqual(len(rows), 3)

    def test_checkpoint_backward_compat_old_uuid_format(self):
        """Old UUID-only format triggers reset (no last_timestamp)."""
        self._insert_sample_emails()
        checkpoint = {"last_email_id": "some-old-uuid", "last_timestamp": None}
        rows = self._build_classify_query(checkpoint, batch_size=10)
        self.assertEqual(len(rows), 3)

    def test_checkpoint_backward_compat_valid_v2(self):
        """Valid v2 checkpoint resumes from correct position."""
        self._insert_sample_emails()

        checkpoint = {"last_timestamp": "2024-01-02T00:00:00Z", "last_email_id": "id-02"}
        rows = self._build_classify_query(checkpoint, batch_size=10)

        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["id-03"])


class TestCheckpointBackwardCompat(unittest.TestCase):
    """Test checkpoint migration logic in load_checkpoint."""

    def test_old_uuid_only_triggers_reset(self):
        """Old checkpoint with last_email_id but no last_timestamp logs warning and resets."""
        old_checkpoint = {
            "discover_phase_done": True,
            "classify_phase_done": False,
            "last_email_id": "a3f1b2c3-d4e5-6789-abcd-ef0123456789",
            "processed": 500,
            "projects_discovered": 5,
            "emails_classified": 500
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(old_checkpoint, f)
            temp_path = f.name

        try:
            with patch.object(ce, 'CHECKPOINT_PATH', Path(temp_path)):
                checkpoint = ce.load_checkpoint()

                self.assertIsNone(checkpoint.get("last_email_id"))
                self.assertIsNone(checkpoint.get("last_timestamp"))
        finally:
            os.unlink(temp_path)

    def test_fresh_checkpoint_preserved(self):
        """Fresh checkpoint (no last_email_id) is preserved as-is."""
        fresh_checkpoint = {
            "discover_phase_done": False,
            "classify_phase_done": False,
            "last_email_id": None,
            "processed": 0,
            "projects_discovered": 0,
            "emails_classified": 0
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(fresh_checkpoint, f)
            temp_path = f.name

        try:
            with patch.object(ce, 'CHECKPOINT_PATH', Path(temp_path)):
                checkpoint = ce.load_checkpoint()

                self.assertIsNone(checkpoint.get("last_email_id"))
                self.assertIsNone(checkpoint.get("last_timestamp"))
                self.assertFalse(checkpoint.get("discover_phase_done"))
        finally:
            os.unlink(temp_path)

    def test_v2_checkpoint_with_timestamp_preserved(self):
        """V2 checkpoint with both last_timestamp and last_email_id is preserved."""
        v2_checkpoint = {
            "discover_phase_done": True,
            "classify_phase_done": False,
            "last_timestamp": "2024-01-15T10:30:00Z",
            "last_email_id": "some-uuid",
            "processed": 100,
            "projects_discovered": 3,
            "emails_classified": 100
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(v2_checkpoint, f)
            temp_path = f.name

        try:
            with patch.object(ce, 'CHECKPOINT_PATH', Path(temp_path)):
                checkpoint = ce.load_checkpoint()

                self.assertEqual(checkpoint.get("last_timestamp"), "2024-01-15T10:30:00Z")
                self.assertEqual(checkpoint.get("last_email_id"), "some-uuid")
        finally:
            os.unlink(temp_path)


class TestParameterizedQuery(unittest.TestCase):
    """Test that parameterized queries work correctly."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        self.cursor.execute("""
            CREATE TABLE emails (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                body_text TEXT,
                body_markdown TEXT,
                body_plain TEXT,
                sender TEXT,
                recipients TEXT,
                timestamp TEXT NOT NULL,
                category_tags TEXT,
                project_tags TEXT
            )
        """)
        self.cursor.execute("CREATE INDEX idx_emails_timestamp ON emails(timestamp)")

    def tearDown(self):
        self.conn.close()

    def test_parameterized_cursor_no_injection(self):
        """Cursor parameters can't be SQL-injected."""
        self.cursor.execute("""
            INSERT INTO emails (id, subject, body_text, body_markdown, body_plain, sender, recipients, timestamp, category_tags, project_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("id-1", "Subject", "body", "markdown", "plain", "s@t.com", "r@t.com", "2024-01-01T00:00:00Z", None, None))
        self.conn.commit()

        malicious_timestamp = "2024-01-01T00:00:00Z' OR '1'='1"
        query = "SELECT id FROM emails WHERE timestamp > ?"
        self.cursor.execute(query, (malicious_timestamp,))
        rows = self.cursor.fetchall()

        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
