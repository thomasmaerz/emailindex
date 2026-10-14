#!/usr/bin/env python3
"""MCP stdio end-to-end test: spawn run-mcp-server.py, assert known queryset.

Deliberately does NOT install opencode or need an LLM. It exercises the real
server entrypoint (schema verify + sqlite-vec load + JSON-RPC framing) against
the deterministic ``ci_db`` fixture via EMAILINDEX_DB_PATH.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import KNOWN_EMAIL_ID, KNOWN_QUERY_TERM

REPO_ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.requires_db


def _send(proc, payload):
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "server closed stdout without responding"
    return json.loads(line)


def _unwrap_tool_result(response):
    text = response["result"]["content"][0]["text"]
    return json.loads(text)


def test_stdio_known_queryset_returns_known_email(db_path):
    env = {
        **os.environ,
        "EMAILINDEX_DB_PATH": str(db_path),
        "HF_HUB_OFFLINE": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "run-mcp-server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    try:
        init_resp = _send(
            proc,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert init_resp["result"]["serverInfo"]["name"] == "email-intelligence"

        query_resp = _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "query_email_database",
                    "arguments": {
                        "exact_keywords": KNOWN_QUERY_TERM,
                        "limit": 5,
                    },
                },
            },
        )
        payload = _unwrap_tool_result(query_resp)
        ids = [r["id"] for r in payload["results"]]
        assert KNOWN_EMAIL_ID in ids, f"known email missing from {ids}"

        get_resp = _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_email_by_id",
                    "arguments": {"email_id": KNOWN_EMAIL_ID},
                },
            },
        )
        email = _unwrap_tool_result(get_resp)
        assert email["id"] == KNOWN_EMAIL_ID
        assert KNOWN_QUERY_TERM in email["subject"]

        thread_resp = _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_thread_by_id",
                    "arguments": {"thread_id": "thread-ci-known-001"},
                },
            },
        )
        thread = _unwrap_tool_result(thread_resp)
        assert thread["thread_id"] == "thread-ci-known-001"
        assert len(thread["emails"]) == 3
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
