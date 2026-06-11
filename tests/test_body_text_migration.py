import pytest
import sqlite3

from migrate_body_text import markdown_contains_formatting, markdown_to_plain_text
from tests.validate_body_text import count_markdown_copies


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Heading\n\nA **bold** [link](https://example.com)", "Heading A bold link"),
        ("- one\n- two", "one two"),
        ("> quoted\n\nplain", "quoted plain"),
    ],
)
def test_markdown_to_plain_text_removes_markdown_syntax(markdown, expected):
    assert markdown_to_plain_text(markdown) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# Heading", True),
        ("Please review [the doc](https://example.com)", True),
        ("Capital ID # 714 was approved.", False),
        ("Plain text only", False),
    ],
)
def test_markdown_contains_formatting_identifies_real_markdown(text, expected):
    assert markdown_contains_formatting(text) is expected


def test_markdown_to_plain_text_keeps_image_only_markdown_empty():
    assert markdown_contains_formatting("![](cid:image001.png@01D2C80A.BA8433A0)") is True
    assert markdown_to_plain_text("![](cid:image001.png@01D2C80A.BA8433A0)") == ""


def test_markdown_to_plain_text_handles_empty_target_links_and_images():
    assert markdown_contains_formatting("![inline]()") is True
    assert markdown_to_plain_text("![inline]()") == "inline"
    assert markdown_to_plain_text("[portal](/path)") == "portal"


def test_count_markdown_copies_ignores_plain_text_hash_characters():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE emails (body_markdown TEXT, body_text TEXT)")
    cur.executemany(
        "INSERT INTO emails(body_markdown, body_text) VALUES (?, ?)",
        [
            ("Capital ID # 714 was approved.", "Capital ID # 714 was approved."),
            ("# Heading\n\nBody", "# Heading\n\nBody"),
            ("Get [Outlook for iOS](https://aka.ms/o0ukef)", "Get [Outlook for iOS](https://aka.ms/o0ukef)"),
        ],
    )

    assert count_markdown_copies(conn) == 2
    conn.close()


def test_count_markdown_copies_ignores_non_transforming_angle_brackets_and_shebangs():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE emails (body_markdown TEXT, body_text TEXT)")
    cur.executemany(
        "INSERT INTO emails(body_markdown, body_text) VALUES (?, ?)",
        [
            ("On Apr 26, 2021, at 03:19 PM, David Harris <david@example.com> wrote:", "On Apr 26, 2021, at 03:19 PM, David Harris <david@example.com> wrote:"),
            ("#!/bin/bash\nset -x", "#!/bin/bash\nset -x"),
            ("[portal](https://example.com)", "[portal](https://example.com)"),
        ],
    )

    assert count_markdown_copies(conn) == 1
    conn.close()
