"""PLAN 7.1/7.2 + audit M-11: every dynamic value interpolated into HTML sinks is escaped.

Static analysis guard (no JS runtime in CI):

- scans EVERY ``static/admin/*.js`` / ``*.html`` file (glob, not a hardcoded
  list — new files cannot silently escape the guard);
- masks comments and string literals, but keeps ``${...}`` interpolation
  regions of template literals visible (audit M-11: whole-literal masking
  made ``innerHTML = `<div>${server.name}</div>`` pass);
- matches assignments with ``=`` AND ``+=`` across ``innerHTML``/``outerHTML``
  and covers ``insertAdjacentHTML``;
- for ``+`` concatenations every operand must be a literal, a constant, or an
  ``esc()``/primitive-wrapper call — one ``esc(`` no longer launders the whole
  statement (property accesses outside ``esc(...)`` spans fail); ternary
  *conditions* are boolean guards, not emitted HTML, so they are excluded;
- every ``${expr}`` inside an HTML sink must be ``esc()``-wrapped or a
  constant;
- raw scan for the remaining sinks: ``insertAdjacentHTML``, ``outerHTML``,
  ``srcdoc``, ``document.write(``, ``javascript:`` URLs and
  ``setAttribute("href"/"src")``.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "admin"
HTML_FILES = sorted(STATIC_DIR.glob("*.html"))
JS_FILES = sorted(STATIC_DIR.glob("*.js"))
ALL_FILES = JS_FILES + HTML_FILES

_JS_TOKEN = re.compile(
    r"//[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r'|"(?:[^"\\\n]|\\.)*"'  # "..."
    r"|'(?:[^'\\\n]|\\.)*'"  # '...'
    r"|`(?:[^`\\]|\\.)*`",  # `...` (template literal)
    re.DOTALL,
)

# innerHTML/outerHTML assignment (incl. +=, audit M-11) and insertAdjacentHTML.
_SINK_ASSIGN = re.compile(r"\.(?:innerHTML|outerHTML)\s*(?:\+=|=)")
_INSERT_SINK = re.compile(r"\.insertAdjacentHTML\s*\(")

# Property/object access — the dangerous class of interpolation operand.
_PROPERTY_ACCESS = re.compile(r"[A-Za-z_$][\w$]*\s*\.\s*[A-Za-z_$][\w$]*")

# Constants tolerated unescaped inside interpolations: pure (masked) string
# text, numbers, boolean/null, ALL_CAPS naming convention, or a wrapped call.
_CONST_EXPR = re.compile(
    r"^(\x00*|\d[\d_]*(?:\.\d+)?|true|false|null|[A-Z][A-Z0-9_]*)$"
)


def _mask_template(body: str) -> str:
    """Blank static text of a template literal but keep ``${...}`` visible."""
    out: list[str] = []
    i = 0
    while i < len(body):
        j = body.find("${", i)
        if j == -1:
            out.append("\x00" * (len(body) - i))
            break
        out.append("\x00" * (j - i))
        depth = 1
        k = j + 2
        while k < len(body) and depth:
            c = body[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            k += 1
        out.append(body[j:k])
        i = k
    return "".join(out)


def _mask(source: str) -> str:
    """Replace comments and string literals with same-length placeholders.

    Template literals are masked *piecewise* so interpolations survive
    (audit M-11 blind spot #1).
    """

    def sub(m: re.Match[str]) -> str:
        g = m.group(0)
        if g.startswith("`"):
            return "`" + _mask_template(g[1:-1]) + "`"
        return '"' + "\x00" * (len(g) - 2) + '"'

    return _JS_TOKEN.sub(sub, source)


def _scripts(path: Path) -> list[tuple[int, str]]:
    """(start_line, code) blocks of JS: whole .js file, or <script> blocks."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        return [(1, text)]
    out = []
    for m in re.finditer(r"<script>(.*?)</script>", text, re.DOTALL):
        out.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return out


def _balanced_region(s: str, start: int, open_tok: str, close_tok: str) -> int:
    """Index just past the balanced close of the region starting at ``start``."""
    depth = 0
    k = start
    step = len(open_tok)
    while k < len(s):
        if s.startswith(open_tok, k):
            depth += 1
            k += step
        elif s.startswith(close_tok, k):
            depth -= 1
            k += 1
            if depth == 0:
                return k
        else:
            k += 1
    return len(s)


def _esc_spans(stmt: str) -> list[tuple[int, int]]:
    """Character spans covered by a balanced ``esc( ... )`` call."""
    return [
        (m.start(), _balanced_region(stmt, m.start(), "(", ")"))
        for m in re.finditer(r"\besc\(", stmt)
    ]


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _strip_range(stmt: str, a: int, b: int) -> tuple[int, int]:
    while a < b and stmt[a].isspace():
        a += 1
    while b > a and stmt[b - 1].isspace():
        b -= 1
    return a, b


def _property_checks(stmt: str, s: int, e: int) -> list[tuple[int, int]]:
    """Ranges of an HTML-sink statement whose values are *emitted*.

    Splits on top-level ``+``, descends fully-parenthesised operands and
    skips ternary conditions — a guard like ``(t.domain ? esc(t.domain) :
    "")`` emits only the two arms, never the tested property (M-11 keeps
    its teeth on every operand that actually reaches the sink).
    """
    s, e = _strip_range(stmt, s, e)
    while s < e and stmt[s] == "(" and _balanced_region(stmt, s, "(", ")") == e:
        s, e = _strip_range(stmt, s + 1, e - 1)
    parts: list[tuple[int, int]] = []
    split = False
    depth = 0
    pending_q = -1
    last = s
    i = s
    while i < e:
        c = stmt[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0:
            if c == "?" and stmt[i + 1 : i + 2] != ".":
                if pending_q == -1:
                    pending_q = i
                    last = i + 1  # condition = dropped segment, not an operand
            elif c == "+":
                parts.append((last, i))
                last = i + 1
                split = True
            elif c == ":" and pending_q != -1:
                parts += _property_checks(stmt, last, i)  # consequent arm
                last = i + 1
                pending_q = -1
                split = True
        i += 1
    if pending_q != -1:  # unterminated (e.g. ``??``) -> check conservatively
        return [(s, e)]
    if not split:
        return [(s, e)] if s < e else []
    out: list[tuple[int, int]] = []
    for a, b in parts:  # condition ranges were dropped above
        out += _property_checks(stmt, a, b)
    return out


def _interpolations(stmt: str) -> list[tuple[int, str]]:
    """Top-level ``${expr}`` regions: (start, expr-without-delimiters)."""
    found: list[tuple[int, str]] = []
    i = 0
    while True:
        j = stmt.find("${", i)
        if j == -1:
            return found
        end = _balanced_region(stmt, j, "${", "}")
        found.append((j, stmt[j + 2 : end - 1]))
        i = end


def _statement_offenders(base_line: int, code: str, masked: str, stmt_start: int, stmt_end: int, label: str) -> list[str]:
    bad: list[str] = []
    stmt = masked[stmt_start:stmt_end]
    raw = code[stmt_start:stmt_end]
    spans = _esc_spans(stmt)

    if "+" in stmt:
        # Every property access that reaches the concatenation must sit
        # inside esc(...) — a single esc() no longer launders the whole
        # statement (M-11 #3). Ternary conditions are emitted as booleans,
        # never as HTML, so they are excluded from the sink operands.
        for a, b in _property_checks(stmt, 0, len(stmt)):
            for m in _PROPERTY_ACCESS.finditer(stmt, a, b):
                if not _in_spans(m.start(), spans):
                    line = base_line + code.count("\n", 0, stmt_start + m.start())
                    bad.append(
                        f"line {line}: {label} concat property {m.group(0)!r} "
                        "outside esc()"
                    )

    for pos, expr in _interpolations(stmt):
        e = expr.strip()
        if e.startswith("esc(") or _CONST_EXPR.match(e):
            continue
        if _PROPERTY_ACCESS.search(e) is not None or re.search(r"[A-Za-z_$]", e):
            line = base_line + code.count("\n", 0, stmt_start + pos)
            bad.append(
                f"line {line}: {label} interpolation ${{{e}}} without esc()"
            )

    if not raw.strip():
        line = base_line + code.count("\n", 0, stmt_start)
        bad.append(f"line {line}: {label} assignment could not be parsed")
    return bad


def _dynamic_html_assignments(path: Path) -> list[str]:
    bad: list[str] = []
    for base_line, code in _scripts(path):
        masked = _mask(code)
        for rx, label in ((_SINK_ASSIGN, "innerHTML/outerHTML"), (_INSERT_SINK, "insertAdjacentHTML")):
            for m in rx.finditer(masked):
                end = masked.find(";", m.end())
                stmt_start, stmt_end = m.end(), end if end != -1 else len(masked)
                if label == "insertAdjacentHTML":
                    stmt_start, stmt_end = m.start(), end if end != -1 else len(masked)
                bad += _statement_offenders(
                    base_line, code, masked, stmt_start, stmt_end, label
                )
    return bad


def test_no_unescaped_html_assignments():
    offenders: list[str] = []
    for path in ALL_FILES:
        offenders += [f"{path.name}: {o}" for o in _dynamic_html_assignments(path)]
    assert not offenders, "\n".join(offenders)


def test_esc_helper_defined_in_scripts():
    assert JS_FILES, "no admin scripts found to guard"
    for path in JS_FILES:
        assert "function esc(" in path.read_text(encoding="utf-8"), path.name


def test_no_other_html_sinks():
    for path in ALL_FILES:
        src = path.read_text(encoding="utf-8")
        for sink in ("insertAdjacentHTML", "outerHTML", "srcdoc", "document.write(", "javascript:"):
            assert sink not in src, f"{path.name}: {sink}"
        assert not re.search(
            r"""setAttribute\(\s*["'](?:href|src)["']""", src
        ), f"{path.name}: setAttribute href/src sink"
