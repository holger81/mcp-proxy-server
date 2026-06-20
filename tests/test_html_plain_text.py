"""Tests for HTML → plain text extraction."""

from __future__ import annotations

from mcp_proxy.html_plain_text import html_to_plain_text


def test_basic_paragraph_and_entities() -> None:
    plain, truncated = html_to_plain_text(
        "<p>Hello <b>world</b> &amp; friends.</p>"
    )
    assert plain == "Hello world & friends."
    assert truncated is False


def test_skips_script_and_style() -> None:
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>Visible</p><script>alert(1)</script></body></html>"
    )
    plain, _ = html_to_plain_text(html)
    assert plain == "Visible"
    assert "alert" not in plain
    assert "color" not in plain


def test_block_elements_add_line_breaks() -> None:
    html = "<div>Line one</div><div>Line two</div><p>Para</p>"
    plain, _ = html_to_plain_text(html)
    assert plain == "Line one\nLine two\nPara"


def test_max_chars_truncates() -> None:
    long = "A" * 200 + ". " + "B" * 200
    plain, truncated = html_to_plain_text(f"<p>{long}</p>", max_chars=120)
    assert truncated is True
    assert len(plain) <= 120


def test_empty_html() -> None:
    plain, truncated = html_to_plain_text("   ")
    assert plain == ""
    assert truncated is False
