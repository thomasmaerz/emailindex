import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import tests.validate_extraction_pipeline as validation


def test_fetch_sample_rows_respects_limit():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("CREATE TABLE emails (id TEXT, subject TEXT, body_markdown TEXT, body_text TEXT, source TEXT)")
    cur.executemany(
        "INSERT INTO emails VALUES (?, ?, ?, ?, ?)",
        [(str(i), f"subject-{i}", "body", "body", "original") for i in range(10)],
    )
    conn.commit()

    rows = validation.fetch_sample_rows(conn, sample_size=3)

    assert len(rows) == 3
    conn.close()


def test_run_validation_sample_mode_skips_full_integrity_checks():
    with patch.object(validation, "check_extraction_quality", return_value=(True, "ok", {})), \
         patch.object(validation, "check_maildir_db_field_preservation", return_value=(True, "ok", {})), \
         patch.object(validation, "check_attachment_pipeline", return_value=(True, "ok", {})), \
         patch.object(validation, "check_vector_coverage", return_value=(True, "ok", {})), \
         patch.object(validation, "check_has_attachments_accuracy", return_value=(True, "ok", {})), \
         patch.object(validation, "check_no_raw_html_in_body", return_value=(True, "ok", {})), \
         patch.object(validation, "check_sample_plaintext_quality", return_value=(True, "ok", {})), \
         patch.object(validation, "check_full_quoted_reply_integrity") as full_check, \
         patch.object(validation, "scan_maildir_count", return_value=0), \
         patch("tests.validate_extraction_pipeline.sqlite3.connect") as connect:
        fake_conn = connect.return_value
        fake_cursor = fake_conn.cursor.return_value
        fake_cursor.fetchone.side_effect = [(0,), (0,), (0,)]

        validation.run_validation(sample_size=5, verbose=False)

    full_check.assert_not_called()


def test_run_validation_full_mode_invokes_full_integrity_checks():
    with patch.object(validation, "check_extraction_quality", return_value=(True, "ok", {})), \
         patch.object(validation, "check_maildir_db_field_preservation", return_value=(True, "ok", {})), \
         patch.object(validation, "check_attachment_pipeline", return_value=(True, "ok", {})), \
         patch.object(validation, "check_vector_coverage", return_value=(True, "ok", {})), \
         patch.object(validation, "check_has_attachments_accuracy", return_value=(True, "ok", {})), \
         patch.object(validation, "check_no_raw_html_in_body", return_value=(True, "ok", {})), \
         patch.object(validation, "check_sample_plaintext_quality", return_value=(True, "ok", {})), \
         patch.object(validation, "check_full_quoted_reply_integrity", return_value=(True, "ok", {})) as full_check, \
         patch.object(validation, "scan_maildir_count", return_value=0), \
         patch("tests.validate_extraction_pipeline.sqlite3.connect") as connect:
        fake_conn = connect.return_value
        fake_cursor = fake_conn.cursor.return_value
        fake_cursor.fetchone.side_effect = [(0,), (0,), (0,)]

        validation.run_validation(sample_size=0, verbose=False)

    full_check.assert_called_once()
