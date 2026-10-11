"""Cleaning raw logs for a non-terminal reader (MCP agent).

- strips ANSI sequences (CSI, OSC, other escapes);
- resolves ``\\r``: only the last state of a rewritten line is kept
  (tqdm progress bars);
- basic handling of ``\\b`` (backspace);
- ``grep`` filtering, keeping the last ``tail`` lines and truncating to a character budget.
"""

from __future__ import annotations

import re

_ANSI_RE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL | ST
    | \x1b\[[0-?]*[ -/]*[@-~]           # CSI
    | \x1b[PX^_][^\x1b]*\x1b\\          # DCS/SOS/PM/APC ... ST
    | \x1b[@-Z\\-_]                     # 2-byte escapes
    | \x9b[0-?]*[ -/]*[@-~]             # 8-bit CSI
    """,
    re.VERBOSE,
)
_CTRL_RE = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")

DEFAULT_MAX_CHARS = 20_000


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences."""
    return _ANSI_RE.sub("", text)


def _render_line(segment: str) -> str:
    """Apply ``\\r`` and ``\\b`` the way a terminal would on a line."""
    if "\r" not in segment and "\b" not in segment:
        return segment
    buf: list[str] = []
    col = 0
    for ch in segment:
        if ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(0, col - 1)
        else:
            if col < len(buf):
                buf[col] = ch
            else:
                buf.append(ch)
            col += 1
    return "".join(buf)


def resolve_carriage_returns(text: str) -> str:
    """Resolve ``\\r`` line by line (``\\r\\n`` is treated as a line ending)."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    out = []
    for line in lines:
        # tqdm often ends with "\r" then rewrites: keep the last non-empty state
        if "\r" in line:
            parts = [p for p in line.split("\r")]
            while len(parts) > 1 and parts[-1] == "":
                parts.pop()
            line = "\r".join(parts)
        out.append(_render_line(line))
    return "\n".join(out)


def clean_logs(raw: bytes | str) -> str:
    """Raw → readable text (ANSI stripped, ``\\r`` resolved, control characters removed)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = strip_ansi(text)
    text = resolve_carriage_returns(text)
    text = _CTRL_RE.sub("", text.replace("\t", "    "))
    return text


def tail_lines(text: str, n: int | None) -> str:
    if n is None or n <= 0:
        return text
    lines = text.rstrip("\n").split("\n")
    return "\n".join(lines[-n:])


def grep_lines(text: str, pattern: str) -> str:
    """Keep the lines matching ``pattern`` (regex, or plain text if the regex is invalid)."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
        keep = [ln for ln in text.split("\n") if rx.search(ln)]
    except re.error:
        low = pattern.lower()
        keep = [ln for ln in text.split("\n") if low in ln.lower()]
    return "\n".join(keep)


def truncate(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Truncate from the start (the end, the most useful part, is kept) with an explicit note."""
    if len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    nl = cut.find("\n")
    if 0 <= nl < 200:
        cut = cut[nl + 1 :]
    return f"[… {len(text) - len(cut)} older characters truncated …]\n{cut}"


def readable_logs(
    raw: bytes | str,
    *,
    tail: int | None = 200,
    grep: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Full pipeline used by the ``get_logs`` MCP tool."""
    text = clean_logs(raw)
    if grep:
        text = grep_lines(text, grep)
    text = tail_lines(text, tail)
    return truncate(text, max_chars)
