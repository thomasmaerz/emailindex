"""Shared pytest fixtures for CI.

Tiered strategy (issue #40):
- PR fast path: deterministic ``ci_db`` session fixture (~12 emails, no
  network). Built via the real ``ingest.init_database`` + ``insert_email``
  pipeline with fixed pseudo-embeddings (384-dim). Covers FTS5, threading,
  pagination, contacts, timelines, and the MCP stdio e2e test.
- Full path: ``enron_db`` fixture attempts a ~2K Enron slice from
  HuggingFace with real ``bge-small-en-v1.5`` embeddings, but ONLY when
  ``EMAILINDEX_FULL_FIXTURE=1``. Otherwise it skips so PRs never depend
  on network/model downloads. Nightly CI sets the env var and uploads
  the resulting DB as an artifact for reuse.
"""

import os
import sqlite3
import struct
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

KNOWN_QUERY_TERM = "zephyrbudget2024"
KNOWN_EMAIL_ID = "11111111-1111-4111-8111-111111111111"
KNOWN_THREAD_ID = "thread-ci-known-001"
KNOWN_CONTACT = "alice.ci@example.com"


def pytest_configure(config):
    config.addinivalue_line("markers", "requires_db: test requires ingested email fixture")


def _pseudo_embedding(seed: int) -> bytes:
    """Deterministic 384-dim float32 vector, no model download needed."""
    vals = [float(((seed * 31 + i * 17) % 100) / 100.0) for i in range(384)]
    return struct.pack("384f", *vals)


def _record(**overrides):
    eid = overrides.get("id", str(uuid.uuid4()))
    base = {
        "id": eid,
        "message_id": f"<{eid}@ci.emailindex.local>",
        "thread_id": KNOWN_THREAD_ID,
        "subject_thread_key": "ci fixture subject",
        "timestamp": "2024-03-15T10:00:00Z",
        "from_address": "bob.ci@example.com",
        "from_name": "Bob CI",
        "to_addresses": '["alice.ci@example.com"]',
        "cc_addresses": None,
        "subject": "CI fixture email",
        "body_markdown": "Hello from the CI fixture.",
        "body_plain": "Hello from the CI fixture.",
        "body_text": "Hello from the CI fixture.",
        "body_main_text": "Hello from the CI fixture.",
        "x_mailer": None,
        "has_attachments": 0,
        "attachments": "[]",
        "folder": "INBOX",
        "raw_eml": None,
        "embedding": _pseudo_embedding(1),
        "source": "original",
        "parent_id": None,
        "content_hash": None,
        "is_outbound": 0,
        "category_tags": '["work"]',
        "sender": "bob.ci@example.com",
        "recipients": '["alice.ci@example.com"]',
        "project_tags": "[]",
    }
    base.update(overrides)
    return base


def _seed_records():
    records = [
        _record(
            id=KNOWN_EMAIL_ID,
            message_id="<known-query@ci.emailindex.local>",
            timestamp="2024-03-15T10:00:00Z",
            from_address=KNOWN_CONTACT,
            from_name="Alice CI",
            subject=f"Quarterly planning {KNOWN_QUERY_TERM}",
            body_markdown=f"Body mentions {KNOWN_QUERY_TERM} for regression search.",
            body_plain=f"Body mentions {KNOWN_QUERY_TERM} for regression search.",
            body_text=f"Body mentions {KNOWN_QUERY_TERM} for regression search.",
            body_main_text=f"Body mentions {KNOWN_QUERY_TERM} for regression search.",
            sender=KNOWN_CONTACT,
            embedding=_pseudo_embedding(7),
        ),
        _record(
            id="22222222-2222-4222-8222-222222222222",
            message_id="<reply-1@ci.emailindex.local>",
            timestamp="2024-03-15T11:00:00Z",
            from_address="bob.ci@example.com",
            subject="Re: Quarterly planning",
            body_markdown="Reply one in known thread.",
            body_plain="Reply one in known thread.",
            body_text="Reply one in known thread.",
            body_main_text="Reply one in known thread.",
            embedding=_pseudo_embedding(8),
        ),
        _record(
            id="33333333-3333-4333-8333-333333333333",
            message_id="<reply-2@ci.emailindex.local>",
            timestamp="2024-03-15T12:00:00Z",
            from_address=KNOWN_CONTACT,
            from_name="Alice CI",
            subject="Re: Quarterly planning",
            body_markdown="Reply two in known thread.",
            body_plain="Reply two in known thread.",
            body_text="Reply two in known thread.",
            body_main_text="Reply two in known thread.",
            sender=KNOWN_CONTACT,
            embedding=_pseudo_embedding(9),
        ),
    ]
    # Pagination / timeline / contact spread: one email per month, alternating senders.
    for i in range(9):
        seed = 100 + i
        month = f"2023-{i + 1:02d}-10T09:00:00Z"
        sender = KNOWN_CONTACT if i % 2 == 0 else "carol.ci@example.com"
        name = "Alice CI" if i % 2 == 0 else "Carol CI"
        eid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"ci-spread-{i}"))
        records.append(
            _record(
                id=eid,
                message_id=f"<spread-{i}@ci.emailindex.local>",
                thread_id=f"thread-ci-spread-{i:03d}",
                subject_thread_key=f"spread topic {i}",
                timestamp=month,
                from_address=sender,
                from_name=name,
                subject=f"Spread email {i} timeline check",
                body_markdown=f"Spread body {i} timeline check.",
                body_plain=f"Spread body {i} timeline check.",
                body_text=f"Spread body {i} timeline check.",
                body_main_text=f"Spread body {i} timeline check.",
                sender=sender,
                embedding=_pseudo_embedding(seed),
            )
        )
    return records


def _build_ci_db(db_path: Path) -> Path:
    import ingest

    ingest.init_database(db_path)
    conn = ingest.get_db_connection(db_path)
    try:
        for record in _seed_records():
            ingest.insert_email(conn, record)
        # One project row so project tools have deterministic data.
        conn.execute(
            "INSERT OR IGNORE INTO project_registry (name, aliases, summary, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("CIProject", '["ciproject"]', "Deterministic CI fixture project", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture(scope="session")
def ci_db(tmp_path_factory):
    """Small deterministic DB for every PR run. No network required."""
    db_path = Path(tmp_path_factory.mktemp("ci_db")) / "ci.db"
    yield _build_ci_db(db_path)
    try:
        if db_path.exists():
            db_path.unlink()
    except OSError:
        pass


@pytest.fixture(scope="session")
def enron_db(tmp_path_factory):
    """Full ~2K Enron fixture with real embeddings.

    Only runs when EMAILINDEX_FULL_FIXTURE=1 (nightly / embedding-code
    changes). Otherwise skips so PRs stay fast and hermetic.
    """
    if os.environ.get("EMAILINDEX_FULL_FIXTURE") != "1":
        pytest.skip("full Enron fixture only runs with EMAILINDEX_FULL_FIXTURE=1")
    try:
        from datasets import load_dataset
    except ImportError:
        pytest.skip("datasets package not installed")
    try:
        ds = load_dataset("enronarchive/mail", split="train", streaming=True)
        rows = []
        for row in ds.take(int(os.environ.get("EMAILINDEX_FULL_COUNT", "2000"))):
            rows.append(row)
    except Exception as exc:
        pytest.skip(f"HuggingFace dataset unavailable: {exc}")

    import ingest

    db_path = Path(tmp_path_factory.mktemp("enron_db")) / "enron.db"
    ingest.init_database(db_path)
    try:
        embedder = ingest.Embedder(
            batch_size=int(os.environ.get("EMAILINDEX_EMBED_BATCH", "32"))
        )
    except Exception as exc:
        pytest.skip(f"embedding model unavailable: {exc}")
    conn = sqlite3.connect(str(db_path))
    try:
        texts, records = [], []
        for i, row in enumerate(rows):
            subject = str(row.get("subject") or "(no subject)")[:200]
            body = str(row.get("body") or row.get("text") or "")[:1500]
            sender = str(row.get("from") or row.get("sender") or "unknown@enron.com")
            eid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"enron-{i}-{sender}"))
            rec = _record(
                id=eid,
                message_id=f"<enron-{i}@ci.emailindex.local>",
                thread_id=f"thread-enron-{i // 10:04d}",
                subject_thread_key=subject.lower()[:100],
                timestamp="2001-06-15T10:00:00Z",
                from_address=sender,
                from_name=None,
                subject=subject,
                body_markdown=body,
                body_plain=body,
                body_text=body,
                body_main_text=body,
                sender=sender,
                embedding=_pseudo_embedding(0),  # replaced below
            )
            texts.append(ingest.generate_embedding_text(rec))
            records.append(rec)
        blobs = embedder.encode_batch(texts)
        for rec, blob in zip(records, blobs):
            rec["embedding"] = blob
            ingest.insert_email(conn, rec)
        conn.commit()
    finally:
        conn.close()
    yield db_path
    try:
        if db_path.exists():
            db_path.unlink()
    except OSError:
        pass


@pytest.fixture()
def db_path(ci_db, monkeypatch):
    """Patch Config.DB_PATH to the CI fixture for the duration of a test."""
    from mcp_server import config as config_module
    from mcp_server import database as database_module

    original = config_module.Config.DB_PATH
    monkeypatch.setattr(config_module.Config, "DB_PATH", ci_db)
    yield ci_db
    monkeypatch.setattr(config_module.Config, "DB_PATH", original)
    try:
        database_module.close_connection()
    except Exception:
        pass
