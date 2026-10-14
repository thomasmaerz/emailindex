#!/usr/bin/env python3
"""Smoke test for the full Enron fixture (issue #40).

Consumes the session-scoped ``enron_db`` fixture: downloads a ~2K Enron
slice from HuggingFace and ingests it with real bge-small-en-v1.5
embeddings. Runs ONLY in the embeddings-full workflow
(EMAILINDEX_FULL_FIXTURE=1); skips everywhere else, including PR-fast.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytestmark = pytest.mark.requires_db


def test_enron_full_fixture_provisions_db_with_real_embeddings(enron_db):
    conn = sqlite3.connect(str(enron_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        assert n >= 100, f"expected full-scale fixture, got {n} rows"

        with_emb = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        assert with_emb == n, f"{n - with_emb}/{n} rows missing embeddings"

        blobs = conn.execute(
            "SELECT embedding FROM emails WHERE embedding IS NOT NULL LIMIT 5"
        ).fetchall()
        assert blobs
        for (blob,) in blobs:
            assert len(blob) == 384 * 4, f"bad embedding size {len(blob)}"

        fts = conn.execute("SELECT COUNT(*) FROM emails_fts").fetchone()[0]
        assert fts == n, f"FTS index incomplete: {fts}/{n}"
    finally:
        conn.close()
