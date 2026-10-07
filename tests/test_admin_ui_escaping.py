"""PLAN 7.1/7.2: every dynamic value interpolated into ``innerHTML`` is escaped.

Static analysis guard (no JS runtime in CI): mask JS comments and string
literals, then inspect each ``innerHTML =`` assignment. Any assignment that
concatenates with ``+`` (i.e. embeds a runtime value into markup) must route
it through ``esc()``. Provably-static assignments are single literals and
contain no ``+`` operator.

PLAN 7.2 moved the page scripts into ``app.js`` / ``login.js`` so the CSP can
drop ``script-src 'unsafe-inline'``; the guard now scans those files too.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "admin"
HTML_FILES = [STATIC_DIR / "index.html", STATIC_DIR / "login.html"]
JS_FILES = [STATIC_DIR / "app.js", STATIC_DIR / "login.js"]
ALL_FILES = HTML_FILES + JS_FILES

_JS_TOKEN = re.compile(
    r"//[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r'|"(?:[^"\\\n]|\\.)*"'  # "..."
    r"|'(?:[^'\\\n]|\\.)*'"  # '...'
    r"|`(?:[^`\\]|\\.)*`",  # `...` (template literal)
    re.DOTALL,
)


def _mask(source: str) -> str:
    """Replace comments and string literals with same-length placeholders."""
    return _JS_TOKEN.sub(lambda m: '"' + "\x00" * (len(m.group(0)) - 2) + '"', source)


def _scripts(path: Path) -> list[tuple[int, str]]:
    """(start_line, code) blocks of JS: whole .js file, or <script> blocks."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        return [(1, text)]
    out = []
    for m in re.finditer(r"<script>(.*?)</script>", text, re.DOTALL):
        out.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return out


def _dynamic_innerhtml_assignments(path: Path) -> list[str]:
    bad: list[str] = []
    for base_line, code in _scripts(path):
        masked = _mask(code)
        for m in re.finditer(r"innerHTML\s*=", masked):
            end = masked.find(";", m.end())
            stmt = masked[m.end() : end if end != -1 else len(masked)]
            if "+" in stmt and "esc(" not in stmt:
                line = base_line + code.count("\n", 0, m.start())
                bad.append(f"line {line}: innerHTML concat without esc()")
    return bad


def test_no_unescaped_innerhtml_concatenations():
    offenders: list[str] = []
    for path in ALL_FILES:
        offenders += [
            f"{path.name}: {o}" for o in _dynamic_innerhtml_assignments(path)
        ]
    assert not offenders, "\n".join(offenders)


def test_esc_helper_defined_in_both_scripts():
    for path in JS_FILES:
        assert "function esc(" in path.read_text(encoding="utf-8"), path.name


def test_no_other_html_sinks():
    for path in ALL_FILES:
        src = path.read_text(encoding="utf-8")
        for sink in ("insertAdjacentHTML", "outerHTML", "document.write("):
            assert sink not in src, f"{path.name}: {sink}"
