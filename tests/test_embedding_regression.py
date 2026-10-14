#!/usr/bin/env python3
"""Embedding-process regression tests (issue #40 follow-up).

- Mocked encode path: verifies batch size plumbing, float32 bytes layout,
  384-dim contract, and embedding-text format WITHOUT downloading the model.
  Runs on every PR.
- Real-model path: verifies actual bge-small-en-v1.5 output shape. Skips
  when the model is not cached (HF_HUB_OFFLINE=1 or no network) so PRs
  never fail on downloads. Nightly full job warms the cache and runs it.
"""

import struct
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.vector_index import VECTOR_BYTES, VECTOR_DIMENSIONS


def test_embedding_text_format_is_stable():
    import ingest

    record = {
        "subject": "Budget review",
        "from_name": "Alice",
        "from_address": "alice@example.com",
        "timestamp": "2024-03-15T10:00:00Z",
        "body_main_text": "numbers",
        "body_text": "numbers",
        "body_markdown": "numbers",
    }
    text = ingest.generate_embedding_text(record)
    assert text.startswith("Subject: Budget review")
    assert "alice@example.com" in text
    assert "2024-03-15" in text


def test_embedder_encode_batch_contract_without_model():
    """Mock SentenceTransformer: assert bytes layout + batch_size plumbing."""
    import ingest

    fake_vecs = np.zeros((3, 384), dtype=np.float32)
    fake_vecs[0, 0] = 1.0
    fake_vecs[1, 1] = 0.5

    class FakeModel:
        def __init__(self):
            self.calls = {}

        def encode(self, texts, batch_size=None, show_progress_bar=False,
                   convert_to_numpy=True):
            self.calls["batch_size"] = batch_size
            self.calls["n"] = len(texts)
            return fake_vecs[: len(texts)]

    fake = FakeModel()
    with patch("ingest.SentenceTransformer", return_value=fake), patch(
        "ingest.resolve_embedding_device", return_value="cpu"
    ):
        embedder = ingest.Embedder(batch_size=16)
        blobs = embedder.encode_batch(["a", "b", "c"])

    assert fake.calls == {"batch_size": 16, "n": 3}
    assert len(blobs) == 3
    for blob in blobs:
        assert isinstance(blob, bytes)
        assert len(blob) == VECTOR_BYTES
    first = np.frombuffer(blobs[0], dtype=np.float32)
    assert first.shape == (VECTOR_DIMENSIONS,)
    assert first[0] == pytest.approx(1.0)


def test_insert_email_rejects_wrong_dimension(ci_db):
    import ingest

    conn = ingest.get_db_connection(ci_db)
    try:
        record = {
            "id": "99999999-9999-4999-8999-999999999999",
            "message_id": "<bad-dim@ci.emailindex.local>",
            "thread_id": "thread-ci-bad",
            "subject_thread_key": "bad",
            "timestamp": "2024-01-01T00:00:00Z",
            "from_address": "x@example.com",
            "from_name": None,
            "to_addresses": '["y@example.com"]',
            "cc_addresses": None,
            "subject": "bad dim",
            "body_markdown": "bad",
            "body_plain": "bad",
            "body_text": "bad",
            "body_main_text": "bad",
            "x_mailer": None,
            "has_attachments": 0,
            "attachments": "[]",
            "folder": "INBOX",
            "raw_eml": None,
            "embedding": b"\x00" * 100,  # wrong size
            "source": "original",
            "parent_id": None,
            "content_hash": None,
            "is_outbound": 0,
            "category_tags": "[]",
            "sender": "x@example.com",
            "recipients": '["y@example.com"]',
            "project_tags": "[]",
        }
        with pytest.raises(ValueError, match="expected"):
            ingest.insert_email(conn, record)
    finally:
        conn.close()


@pytest.mark.requires_db
def test_real_model_output_shape_when_cached():
    """Runs only when the model is already cached; skips otherwise."""
    import os

    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        pytest.skip("sentence-transformers not installed")
    try:
        model = SentenceTransformer("BAAI/bge-small-en-v1.5", device="cpu")
    except Exception as exc:
        pytest.skip(f"model not cached: {exc}")
    vec = model.encode("CI shape probe", convert_to_numpy=True,
                       show_progress_bar=False)
    assert vec.shape == (VECTOR_DIMENSIONS,)
    assert struct.pack("384f", *vec.astype("float32")).__len__() == VECTOR_BYTES
