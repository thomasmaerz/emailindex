import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ingest


def test_cleanup_image_placeholders_drops_generic_signature_logo_markers():
    cleaned = ingest._cleanup_image_placeholders("signature logo image001")
    assert cleaned.strip() == ""


def test_cleanup_image_placeholders_keeps_meaningful_alt_text():
    cleaned = ingest._cleanup_image_placeholders("OneLogin Logo")
    assert "OneLogin Logo" in cleaned
