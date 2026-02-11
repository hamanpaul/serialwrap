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
