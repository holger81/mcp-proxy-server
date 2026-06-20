"""Extract readable plain text from HTML strings."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_WS_RE = re.compile(r"[ \t]+")
_MULTINL_RE = re.compile(r"\n{3,}")
_TAG_RE = re.compile(r"<[^>]+>")

_BLOCK_BREAK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SKIP = frozenset({"head", "noscript", "script", "style", "template"})


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t == "br":
            self._parts.append("\n")
        elif t in _BLOCK_BREAK and self._parts and self._parts[-1] not in ("\n", ""):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if t in _BLOCK_BREAK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _normalize_plain_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = _WS_RE.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    out = "\n".join(lines)
    out = _MULTINL_RE.sub("\n\n", out).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", out)


def _strip_tags_fallback(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    return html.unescape(text)


def html_to_plain_text(raw: str, *, max_chars: int = 0) -> tuple[str, bool]:
    """Return ``(plain_text, truncated)``. ``max_chars`` 0 means no limit."""
    if not raw or not raw.strip():
        return "", False

    parser = _PlainTextHTMLParser()
    try:
        parser.feed(raw)
        parser.close()
        text = html.unescape(parser.text())
    except Exception:
        text = _strip_tags_fallback(raw)

    text = _normalize_plain_text(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False

    cut = text[:max_chars]
    for sep in (". ", "! ", "? ", "\n"):
        pos = cut.rfind(sep)
        if pos >= max_chars // 2:
            snippet = cut[: pos + len(sep)].strip()
            if len(snippet) <= max_chars:
                return snippet, True
    suffix = "…"
    budget = max_chars - len(suffix)
    if budget < 1:
        return cut[:max_chars], True
    return cut[:budget].rstrip() + suffix, True
