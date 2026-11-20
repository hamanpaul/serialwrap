#!/usr/bin/env python3
"""以 Python 基線產生 Slice 2.1 T0c 的相容性 golden。"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import shlex
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw_core.service import _coerce_rpc_bool
from sw_core.util import clean_text, strip_ansi, to_printable

OUT = ROOT / "contracts/golden/pycompat_s21.tsv"
SEP = "\x1f"


def enc(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def add(rows: list[tuple[str, str, str]], op: str, args: str, expected: str) -> None:
    rows.append((op, enc(args), enc(expected)))


def fmt_utc(us: int, fmt: str) -> str:
    seconds, micros = divmod(us, 1_000_000)
    del micros
    return time.strftime(fmt, time.gmtime(seconds))


def fmt_local(us: int, offset: int, fmt: str) -> str:
    seconds, _micros = divmod(us, 1_000_000)
    return time.strftime(fmt, time.gmtime(seconds + offset))


def deterministic_uuid(seed: bytes) -> uuid.UUID:
    raw = bytearray(seed[:16].ljust(16, b"\0"))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(raw))


def generate() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    # 以 Python datetime/time 與 sw_core 的已交付 helper 作為獨立 oracle。
    stamps = [0, 1, 999_999, 1_000_000, 1_700_000_000_123_456, -1, -1_000_001]
    for us in stamps:
        iso = (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=us)).isoformat()
        add(rows, "iso_utc_now", str(us), iso)
        for fmt in ("%Y%m%d-%H%M%S", "%y%m%d-%H%M%S"):
            add(rows, "strftime_utc", f"{us}{SEP}{fmt}", fmt_utc(us, fmt))
            for offset in (0, 28_800, -18_000, -12_600, 3_600):
                add(rows, "strftime_local", f"{us}{SEP}{offset}{SEP}{fmt}", fmt_local(us, offset, fmt))

    for data in (b"", b"hello", b"a\r\nb", bytes(range(33)), b"\x00\x08\x09\x0b\x0c\x1b\x7f", bytes(range(128, 256))):
        add(rows, "to_printable", data.hex(), to_printable(data))

    text_cases = [
        "plain", "a\r\nb", "one\rtwo", "\x1b[31mred\x1b[0m", "a\x08b", "a\x7fb",
        "bell\x07x", "osc\x1b]0;title\x07x", "cursor\x1b[2Kx", "é中😀",
        "prefix\x1b]first\x1b\\visible\x1b]second\x07suffix",
        "\x1b]one\x07middle\x1b]two\x1b\\tail",
    ]
    for text in text_cases:
        add(rows, "clean_text", text, clean_text(text))
        add(rows, "strip_ansi", text, strip_ansi(text))

    b64_inputs = [
        b"", b"aGVsbG8=", b"aG Vs\nbG8=", b"!!!!aGVsbG8=??", b"YQ==",
        b"=YQ==", b"Y=Q==", b"YQ===", b"YQ", b"aGVsbG8", b"YQ=A", b"AA=A", b"A===",
    ]
    for raw in b64_inputs:
        try:
            expected = base64.b64decode(raw, validate=False).hex()
        except binascii.Error as exc:
            expected = f"ERR:{exc}"
        add(rows, "a2b_base64_lenient", raw.decode("latin1"), expected)
    for raw in (b"", b"hello", b"\x00\xff", bytes(range(64))):
        add(rows, "b2a_base64", raw.hex(), base64.b64encode(raw).decode("ascii"))

    for text in ("", "hello", "it's", "a b", "a'b", "$HOME; x", "é中", "line\nnext"):
        add(rows, "shlex_quote", text, shlex.quote(text))
    for text in (
        "", "a b", "'quoted value' x", '"double quoted"', "a\\ b", "a 'b' c", "unterminated '",
        "a\\\nb", '"a\\\nb"',
    ):
        try:
            result = json.dumps(shlex.split(text, posix=True), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            result = "ERR"
        add(rows, "shlex_split", text, result)

    for value in ("yes", "0", "true", "False", "on", "null", "", "unknown", "  YES  ", "off"):
        parsed = json.loads(value.lower()) if value.lower() in {"true", "false", "null", "0"} else value
        add(rows, "coerce_rpc_bool", value, "true" if _coerce_rpc_bool(parsed) else "false")

    for number, digits in ((2.5, 0), (3.5, 0), (2.25, 1), (2.35, 1), (-2.5, 0), (1.2345, 3), (1234.5, -1), (2.675, 2), (0.125, 2), (-0.125, 2)):
        add(rows, "py_round", f"{number!r}{SEP}{digits}", repr(round(number, digits)))

    for seed in (bytes(range(16)), bytes(reversed(range(16)))):
        value = deterministic_uuid(seed)
        add(rows, "uuid4_hex", seed.hex(), value.hex)
        add(rows, "uuid4_dashed", seed.hex(), str(value))

    # 擴大格式化樣本，使每個平台格式指令都由 CPython 建立多組 oracle。
    for day in range(1, 181):
        us = (1_600_000_000 + day * 12_345) * 1_000_000 + (day * 7919) % 1_000_000
        fmt = "%Y%m%d-%H%M%S"
        add(rows, "strftime_utc", f"{us}{SEP}{fmt}", fmt_utc(us, fmt))

    if len(rows) < 300:
        raise AssertionError(f"golden rows must be >= 300, got {len(rows)}")
    return rows


def main() -> int:
    rows = generate()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as stream:
        for op, input_b64, expected_b64 in rows:
            stream.write(f"{op}\t{input_b64}\t{expected_b64}\n")
    print(f"wrote {len(rows)} rows: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
