#!/usr/bin/env python3
"""
Unit tests for MCP tools: get_email_by_id, get_thread_by_id, list_projects
"""

import sys
import os
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import MCPServer
from mcp_server.database import get_email, get_conversation, list_projects
from mcp_server.config import Config


def create_test_db_with_emails():
    """Create a temporary test database with sample emails."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE emails (
            id TEXT PRIMARY KEY,
            message_id TEXT UNIQUE NOT NULL,
            thread_id TEXT,
            subject_thread_key TEXT,
            timestamp TEXT NOT NULL,
            from_address TEXT NOT NULL,
            from_name TEXT,
            to_addresses TEXT NOT NULL,
            cc_addresses TEXT,
            subject TEXT NOT NULL,
            body_markdown TEXT NOT NULL,
            body_plain TEXT,
            x_mailer TEXT,
            has_attachments INTEGER NOT NULL DEFAULT 0,
            attachments TEXT,
            folder TEXT NOT NULL,
            raw_eml BLOB,
            source TEXT DEFAULT 'original',
            parent_id TEXT,
            content_hash TEXT,
            sender TEXT,
            recipients TEXT,
            body_text TEXT,
            body_main_text TEXT,
            category_tags TEXT,
            project_tags TEXT,
            is_outbound INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_registry (
            name TEXT PRIMARY KEY,
            aliases TEXT,
            summary TEXT,
            created_at TEXT
        )
    """)
    
    test_email_id = "550e8400-e29b-41d4-a716-446655440000"
    test_thread_id = "thread-abc123def456"
    
    cursor.execute("""
        INSERT INTO emails (id, message_id, thread_id, subject_thread_key, timestamp, 
            from_address, from_name, to_addresses, subject, body_markdown, 
            body_text, folder, sender, recipients, has_attachments)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        test_email_id,
        "<test@example.com>",
        test_thread_id,
        "test subject",
        "2024-01-15T10:00:00Z",
        "alice@example.com",
        "Alice",
        '["bob@example.com"]',
        "Test Subject",
        "# Test Body\n\nThis is the test body.",
        "Test Body",
        "INBOX",
        "alice@example.com",
        '["bob@example.com"]',
        0
    ))
    
    cursor.execute("""
        INSERT INTO emails (id, message_id, thread_id, subject_thread_key, timestamp, 
            from_address, from_name, to_addresses, subject, body_markdown, 
            body_text, folder, sender, recipients, has_attachments)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "660e8400-e29b-41d4-a716-446655440001",
        "<test2@example.com>",
        test_thread_id,
        "test subject",
        "2024-01-15T11:00:00Z",
        "bob@example.com",
        "Bob",
        '["alice@example.com"]',
        "Re: Test Subject",
        "# Reply\n\nThis is a reply.",
        "Reply",
        "INBOX",
        "bob@example.com",
        '["alice@example.com"]',
        0
    ))
    
    cursor.execute("""
        INSERT INTO project_registry (name, aliases, summary, created_at)
        VALUES (?, ?, ?, ?)
    """, ("ProjectAlpha", "alpha,project-a", "First test project", "2024-01-01T00:00:00Z"))
    
    cursor.execute("""
        INSERT INTO project_registry (name, aliases, summary, created_at)
        VALUES (?, ?, ?, ?)
    """, ("ProjectBeta", "beta", "Second test project", "2024-02-01T00:00:00Z"))
    
    conn.commit()
    conn.close()
    
    return path, test_email_id, test_thread_id


def test_get_email_by_id_valid():
    """Test get_email_by_id returns full EmailRecord."""
    path, test_id, _ = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        result = get_email(test_id)
        
        assert result is not None, "Expected email record, got None"
        assert result.id == test_id
        assert result.subject == "Test Subject"
        assert result.from_address == "alice@example.com"
        assert "Test Body" in result.body_markdown
        
        print("✓ test_get_email_by_id_valid passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_get_email_by_id_not_found():
    """Test get_email_by_id returns None for invalid UUID."""
    path, _, _ = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        result = get_email("00000000-0000-0000-0000-000000000000")
        
        assert result is None, f"Expected None for invalid UUID, got {result}"
        
        print("✓ test_get_email_by_id_not_found passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_get_thread_by_id_valid():
    """Test get_thread_by_id returns ConversationThread with metadata."""
    path, _, test_thread = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        result = get_conversation(test_thread)
        
        assert result is not None, "Expected thread, got None"
        assert result.thread_id == test_thread
        assert result.subject == "Test Subject"
        assert result.participant_count >= 2
        assert len(result.emails) == 2
        assert result.date_range[0] == "2024-01-15T10:00:00Z"
        assert result.date_range[1] == "2024-01-15T11:00:00Z"
        
        print("✓ test_get_thread_by_id_valid passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_get_thread_by_id_not_found():
    """Test get_thread_by_id returns None for nonexistent thread."""
    path, _, _ = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        result = get_conversation("thread-nonexistent")
        
        assert result is None, f"Expected None for nonexistent thread, got {result}"
        
        print("✓ test_get_thread_by_id_not_found passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_list_projects_returns_array():
    """Test list_projects returns array with parsed aliases."""
    path, _, _ = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        results = list_projects(limit=20)
        
        assert isinstance(results, list), f"Expected list, got {type(results)}"
        assert len(results) == 2
        
        project_names = [p["name"] for p in results]
        assert "ProjectAlpha" in project_names
        assert "ProjectBeta" in project_names
        
        alpha = next(p for p in results if p["name"] == "ProjectAlpha")
        assert isinstance(alpha["aliases"], list), "aliases should be parsed as list"
        assert "alpha" in alpha["aliases"]
        assert "project-a" in alpha["aliases"]
        
        print("✓ test_list_projects_returns_array passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_list_projects_empty_registry():
    """Test list_projects returns empty list, not error, when no projects."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_registry (
                name TEXT PRIMARY KEY,
                aliases TEXT,
                summary TEXT,
                created_at TEXT
            )
        """)
        
        conn.commit()
        conn.close()
        
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        results = list_projects(limit=20)
        
        assert results == [], f"Expected empty list, got {results}"
        
        print("✓ test_list_projects_empty_registry passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_list_projects_limit_respected():
    """Test list_projects respects limit parameter."""
    path, _, _ = create_test_db_with_emails()
    
    try:
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        results = list_projects(limit=1)
        
        assert len(results) <= 1, f"Expected at most 1 result, got {len(results)}"
        
        print("✓ test_list_projects_limit_respected passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_mcp_server_tools_list():
    """Test that MCPServer exposes all 5 tools."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE emails (
                id TEXT PRIMARY KEY,
                message_id TEXT UNIQUE NOT NULL,
                thread_id TEXT,
                subject_thread_key TEXT,
                timestamp TEXT NOT NULL,
                from_address TEXT NOT NULL,
                from_name TEXT,
                to_addresses TEXT NOT NULL,
                cc_addresses TEXT,
                subject TEXT NOT NULL,
                body_markdown TEXT NOT NULL,
                body_plain TEXT,
                x_mailer TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0,
                attachments TEXT,
                folder TEXT NOT NULL,
                raw_eml BLOB,
                sender TEXT,
                recipients TEXT,
                body_text TEXT,
                body_main_text TEXT,
                category_tags TEXT,
                project_tags TEXT,
                is_outbound INTEGER
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_registry (
                name TEXT PRIMARY KEY,
                aliases TEXT,
                summary TEXT,
                created_at TEXT
            )
        """)
        
        conn.commit()
        conn.close()
        
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        server = MCPServer()
        
        expected_tools = [
            "query_email_database",
            "get_project_context",
            "get_email_by_id",
            "get_thread_by_id",
            "list_projects",
            "get_mention_timeline",
            "get_contact_profile",
            "get_thread_arc"
        ]
        
        for tool in expected_tools:
            assert tool in server.tools, f"Tool {tool} not in server.tools"
        
        response = server.handle_request({
            "method": "tools/list",
            "id": 1
        })
        
        tool_names = [t["name"] for t in response["result"]["tools"]]
        for tool in expected_tools:
            assert tool in tool_names, f"Tool {tool} not in tools/list response"
        
        print("✓ test_mcp_server_tools_list passed")
    finally:
        Config.DB_PATH = original_db
        os.unlink(path)


def test_is_outbound_filter():
    """Test that is_outbound filter works in query_email_database."""
    path, test_id, test_thread_id = create_test_db_with_emails()
    
    try:
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        
        cursor.execute("UPDATE emails SET is_outbound = 1 WHERE from_address = 'alice@example.com'")
        cursor.execute("UPDATE emails SET is_outbound = 0 WHERE from_address = 'bob@example.com'")
        conn.commit()
        conn.close()
        
        original_db = Config.DB_PATH
        Config.DB_PATH = Path(path)
        
        try:
            from mcp_server.database import query_email_database
            
            result_outbound = query_email_database(is_outbound=True)
            assert len(result_outbound["results"]) == 1, f"Expected 1 outbound, got {len(result_outbound['results'])}"
            assert result_outbound["results"][0]["from_address"] == "alice@example.com"
            
            result_inbound = query_email_database(is_outbound=False)
            assert len(result_inbound["results"]) == 1, f"Expected 1 inbound, got {len(result_inbound['results'])}"
            assert result_inbound["results"][0]["from_address"] == "bob@example.com"
            
            print("✓ test_is_outbound_filter passed")
        finally:
            Config.DB_PATH = original_db
    finally:
        os.unlink(path)
if __name__ == "__main__":
    print("Running MCP Tools tests...\n")
    
    test_get_email_by_id_valid()
    test_get_email_by_id_not_found()
    test_get_thread_by_id_valid()
    test_get_thread_by_id_not_found()
    test_list_projects_returns_array()
    test_list_projects_empty_registry()
    test_list_projects_limit_respected()
    test_mcp_server_tools_list()
    test_is_outbound_filter()
    
    print("\n✅ All tests passed!")