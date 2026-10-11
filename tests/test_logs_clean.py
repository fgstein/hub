from __future__ import annotations

from trainhub.core.logs_clean import clean_logs, readable_logs, strip_ansi, truncate


def test_strip_ansi() -> None:
    raw = "\x1b[32mINFO\x1b[0m done \x1b]0;title\x07ok \x1b[2K\x1b[1Gx"
    assert strip_ansi(raw) == "INFO done ok x"


def test_progress_bar_keeps_last_state() -> None:
    raw = b"Epoch 1:  10%|#  |\rEpoch 1:  50%|##### |\rEpoch 1: 100%|##########|\r\nnext\n"
    assert clean_logs(raw) == "Epoch 1: 100%|##########|\nnext\n"


def test_trailing_cr_and_overwrite() -> None:
    assert clean_logs("abcdef\rxy\n") == "xycdef\n"
    assert clean_logs("loading 1\rloading 2\r") == "loading 2"


def test_backspace() -> None:
    assert clean_logs("abc\b\bX\n") == "aXc\n"


def test_readable_logs_tail_grep_truncate() -> None:
    raw = "".join(f"line {i}\n" for i in range(1000))
    out = readable_logs(raw, tail=3)
    assert out == "line 997\nline 998\nline 999"
    assert readable_logs(raw, tail=None, grep=r"line 99\d$").splitlines()[0] == "line 990"
    long = readable_logs(raw, tail=None, max_chars=100)
    assert long.startswith("[…") and "truncated" in long and long.endswith("line 999\n")


def test_truncate_noop() -> None:
    assert truncate("abc", 10) == "abc"


def test_invalid_regex_falls_back_to_substring() -> None:
    assert readable_logs("a[b\nc\n", grep="[b") == "a[b"
