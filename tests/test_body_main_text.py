import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ingest


def test_body_main_text_removes_outlook_reply_headers_when_new_text_exists():
    body_text = "Hi team, please review.\n\nFrom: Alice\nSent: Monday\nTo: Bob\nSubject: RE: Example"

    cleaned = ingest._derive_body_main_text(body_text=body_text, body_markdown="")

    assert "Hi team, please review." in cleaned
    assert "From: Alice" not in cleaned


def test_body_main_text_reduces_signature_and_disclaimer_noise():
    body_text = "Thanks,\nThomas\n\nCONFIDENTIALITY NOTICE This message..."

    cleaned = ingest._derive_body_main_text(body_text=body_text, body_markdown="")

    assert "Thanks" in cleaned
    assert "CONFIDENTIALITY NOTICE" not in cleaned


def test_body_main_text_flattens_table_junk_for_machine_generated_mail():
    body_markdown = "| Subject: | Example |\n| --- | --- |\n| Status: | Approved |"

    cleaned = ingest._derive_body_main_text(body_text="", body_markdown=body_markdown)

    assert "Subject: Example" in cleaned
    assert "Status: Approved" in cleaned
    assert "| --- |" not in cleaned


def test_body_main_text_preserves_forwards_when_no_new_text_exists():
    body_text = "Sent from my iPhone\n\nBegin forwarded message:\n\nFrom: Nancy Deardeuff\nDate: August 19, 2015\nTo: Michelle\nSubject: Order Management\n\nMichelle,\nThank you."
    cleaned = ingest._derive_body_main_text(body_text=body_text, body_markdown="")
    assert "Nancy Deardeuff" in cleaned
    assert "Thank you" in cleaned


def test_body_main_text_falls_back_to_basic_if_aggressive_cleaning_empties_content():
    body_text = "Get Outlook for iOS"
    cleaned = ingest._derive_body_main_text(body_text=body_text, body_markdown="")
    assert "Get Outlook" in cleaned
