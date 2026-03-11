from __future__ import annotations

import datetime as _dt
import json
import os
import re
import string
import time
from typing import Any


def now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def monotonic_ns() -> int:
    return time.monotonic_ns()


def dumps_stable(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def to_printable(data: bytes) -> str:
    out: list[str] = []
    printable = set(string.printable)
    for b in data:
        ch = chr(b)
        if ch in printable and ch not in {"\x0b", "\x0c"}:
            out.append(ch)
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[A-Za-z]")
_OSC_RE = re.compile(r"\x1B\][^\x07]*(\x07|\x1B\\)")
_TRAILING_CONTINUATION_RE = re.compile(r"(?:\|\|?|\&\&)\s*$")


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "")
    text = _ANSI_RE.sub("", text)
    text = _OSC_RE.sub("", text)

    out: list[str] = []
    for ch in text:
        if ch in ("\b", "\x7f"):
            if out:
                out.pop()
            continue
        if ch == "\a":
            continue
        out.append(ch)
    return "".join(out)


def shell_command_incomplete_reason(command: str) -> str | None:
    text = command.rstrip()
    if not text:
        return "EMPTY_COMMAND"
    if "\x00" in text:
        return "NUL_BYTE"

    in_single = False
    in_double = False
    in_backtick = False
    escaped = False

    for ch in text:
        if in_single:
            if ch == "'":
                in_single = False
            continue

        if in_double:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_double = False
            continue

        if in_backtick:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == "`":
                in_backtick = False
            continue

        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue

    if in_single:
        return "UNBALANCED_SINGLE_QUOTE"
    if in_double:
        return "UNBALANCED_DOUBLE_QUOTE"
    if in_backtick:
        return "UNBALANCED_BACKTICK"

    if _TRAILING_CONTINUATION_RE.search(text):
        return "TRAILING_OPERATOR"

    stripped = text.rstrip()
    if stripped.endswith("\\"):
        slash_count = 0
        idx = len(stripped) - 1
        while idx >= 0 and stripped[idx] == "\\":
            slash_count += 1
            idx -= 1
        if slash_count % 2 == 1:
            return "TRAILING_BACKSLASH"

    return None
