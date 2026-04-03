#!/usr/bin/env python3
"""
Unit tests for quote salvage pipeline:
- Outlook pattern detection
- EmailReplyParser integration
- Tier 1 (hash) deduplication
- Tier 2 (semantic similarity) deduplication
"""
import json
import sqlite3
import tempfile
import pytest
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from ingest import (
    salvage_quotes,
    _extract_outlook_quotes,
    _compute_content_hash,
    _is_duplicate_by_hash,
    _is_duplicate_by_similarity,
    init_database,
    get_db_connection,
    _encode_text_to_embedding,
    insert_email,
)

SAMPLE_PARENT = {
    'id': 'test-parent-001',
    'message_id': 'test@example.com',
    'thread_id': 'thread-test-001',
    'subject_thread_key': 'test subject',
    'timestamp': '2024-01-15T10:30:00Z',
    'from_address': 'alice@example.com',
    'from_name': 'Alice',
    'to_addresses': json.dumps(['bob@example.com']),
    'cc_addresses': None,
    'subject': 'Test Subject',
    'body_markdown': '',
    'body_plain': '',
    'x_mailer': None,
    'has_attachments': 0,
    'attachments': '[]',
    'folder': 'INBOX',
    'raw_eml': None,
    'embedding': None,
    'source': 'original',
    'parent_id': None,
    'content_hash': None,
}


def _setup_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a test database with the parent email."""
    init_database(db_path)
    conn = get_db_connection(db_path)
    conn.row_factory = sqlite3.Row
    
    parent = SAMPLE_PARENT.copy()
    parent['to_addresses'] = json.dumps(['bob@example.com'])
    
    text = f"Subject: {parent['subject']} | From: {parent['from_name']} <{parent['from_address']}> | Date: {parent['timestamp'][:10]} | Body: test content"
    embedding = _encode_text_to_embedding(text)
    parent['embedding'] = embedding
    
    insert_email(conn, parent)
    conn.commit()
    return conn


class TestOutlookQuoteDetection:
    def test_from_sent_to_subject_pattern(self):
        text = """Reply to this message.

From: Bob Smith <bob@example.com>
Sent: Monday, January 15, 2024 10:30 AM
To: Alice Jones <alice@example.com>
Subject: Meeting Tomorrow

Hi Alice,
Can we meet tomorrow at 2pm?
Thanks,
Bob"""
        quotes = _extract_outlook_quotes(text)
        assert len(quotes) == 1
        assert 'From: Bob Smith' in quotes[0]
        assert 'Meeting Tomorrow' in quotes[0]
    
    def test_multiple_outlook_quotes(self):
        text = """Top level reply.

From: Alice <alice@example.com>
Sent: Monday, January 15, 2024 10:30 AM
To: Bob <bob@example.com>
Subject: Thread

Alice's reply.

From: Bob <bob@example.com>
Sent: Monday, January 14, 2024 9:00 AM
To: Alice <alice@example.com>
Subject: Thread

Bob's original message."""
        quotes = _extract_outlook_quotes(text)
        assert len(quotes) >= 1
    
    def test_original_message_pattern(self):
        text = """Reply text.

-----Original Message-----
From: Bob Smith
Sent: Monday, January 15, 2024 10:30 AM
To: Alice Jones
Subject: Test

I wanted to follow up on our discussion about the quarterly planning meeting. We discussed budget allocations and resource planning for Q1 2024. Please review the attached documents."""
        quotes = _extract_outlook_quotes(text)
        assert len(quotes) >= 1
        assert 'From: Bob Smith' in quotes[0]
    
    def test_on_wrote_pattern(self):
        text = """My reply.

On Mon, Jan 15, 2024 at 10:30 AM, Bob Smith <bob@example.com> wrote:

Hi Alice,

I wanted to follow up on our discussion about the quarterly planning meeting. We discussed budget allocations and resource planning for Q1 2024. Please review the attached documents and let me know your thoughts. I have also included some additional analysis for your review. The key points from our discussion included timeline adjustments, resource allocation changes, and new deliverables. Please let me know if you need any clarification on these items. We should schedule a follow-up meeting to finalize the details. Looking forward to your response.

Best regards,
Bob"""
        quotes = _extract_outlook_quotes(text)
        assert len(quotes) >= 1
        assert 'wrote:' in quotes[0]
    
    def test_short_content_filtered(self):
        text = """From: A
Sent: B
To: C
Subject: D
X"""
        quotes = _extract_outlook_quotes(text)
        assert len(quotes) == 0


class TestTier1Deduplication:
    def test_exact_duplicate_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            content = "This is a quoted reply that should be deduplicated."
            content_hash = _compute_content_hash(content)
            
            assert not _is_duplicate_by_hash(conn, content_hash)
            
            c = conn.cursor()
            c.execute("INSERT INTO emails (id, message_id, thread_id, timestamp, from_address, from_name, to_addresses, subject, body_markdown, body_plain, has_attachments, folder, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ('test-001', 'test-001@example.com', 'thread-test-001', '2024-01-15T10:30:00Z', 'test@test.com', 'Test', '[]', 'Test', content, content, 0, 'INBOX', content_hash))
            conn.commit()
            
            assert _is_duplicate_by_hash(conn, content_hash)
            conn.close()


class TestTier2SemanticDeduplication:
    def test_semantically_similar_text_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            parent_text = "This is a quoted reply about the quarterly planning meeting. We discussed budget allocations and resource planning for Q1 2024."
            parent_embedding = _encode_text_to_embedding(parent_text)
            
            c = conn.cursor()
            c.execute("UPDATE emails SET embedding = ? WHERE id = ?", (parent_embedding, 'test-parent-001'))
            conn.commit()
            
            similar_text = "This is a quoted reply about the quarterly planning meeting. We discussed budget allocations and resource planning for Q1 2024. We also reviewed the quarterly allocations."
            
            assert _is_duplicate_by_similarity(conn, similar_text, 'thread-test-001')
            conn.close()
    
    def test_different_text_not_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            parent_text = "This is about quarterly planning meeting budget."
            parent_embedding = _encode_text_to_embedding(parent_text)
            
            c = conn.cursor()
            c.execute("UPDATE emails SET embedding = ? WHERE id = ?", (parent_embedding, 'test-parent-001'))
            conn.commit()
            
            different_text = "The weather forecast for tomorrow is sunny with a chance of rain in the afternoon."
            
            assert not _is_duplicate_by_similarity(conn, different_text, 'thread-test-001')
            conn.close()


class TestSalvageQuotesIntegration:
    def test_outlook_fallback_creates_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            parent = SAMPLE_PARENT.copy()
            parent['body_plain'] = """Reply to this.

From: Bob Smith <bob@example.com>
Sent: Monday, January 15, 2024 10:30 AM
To: Alice Jones <alice@example.com>
Subject: Meeting Tomorrow

Hi Alice,
Can we meet tomorrow at 2pm? I'd like to discuss the project timeline and deliverables.
Thanks,
Bob"""
            parent['to_addresses'] = json.dumps(['bob@example.com'])
            
            records = salvage_quotes(parent['body_plain'], parent, conn)
            
            assert len(records) >= 1
            assert records[0]['source'] == 'quoted_reply'
            assert records[0]['parent_id'] == 'test-parent-001'
            assert records[0]['content_hash'] is not None
            assert len(records[0]['body_markdown']) > 100
            conn.close()
    
    def test_no_plain_body_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            parent = SAMPLE_PARENT.copy()
            parent['body_plain'] = ''
            parent['to_addresses'] = json.dumps(['bob@example.com'])
            
            records = salvage_quotes(parent['body_plain'], parent, conn)
            assert len(records) == 0
            conn.close()
    
    def test_dedup_prevents_duplicate_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = _setup_test_db(db_path)
            
            parent = SAMPLE_PARENT.copy()
            parent['body_plain'] = """Reply.

From: Bob <bob@example.com>
Sent: Monday, January 15, 2024 10:30 AM
To: Alice <alice@example.com>
Subject: Test

This is the quoted content that should be salvaged."""
            parent['to_addresses'] = json.dumps(['bob@example.com'])
            
            records1 = salvage_quotes(parent['body_plain'], parent, conn)
            assert len(records1) >= 1
            
            for rec in records1:
                rec['to_addresses'] = json.dumps(['bob@example.com'])
                insert_email(conn, rec)
            conn.commit()
            
            records2 = salvage_quotes(parent['body_plain'], parent, conn)
            assert len(records2) == 0
            conn.close()
