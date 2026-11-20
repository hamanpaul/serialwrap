#!/usr/bin/env python3
"""產生 ``contracts/golden/shlex_split.tsv``（P6 Task 3；CPython 3.12 ``shlex.split(s, posix=True)`` 為基準）。

四欄 TSV（P3 ``_tsv.write_tsv``）：``<case_id>\\tshlex_split\\t<hex(輸入 UTF-8)>\\t<hex(expected UTF-8)>``，
expected 為 ``OK:`` 加上以 ``\\x1f`` 串接的 tokens，或 ``ERR:<str(ValueError)>``
（``No closing quotation``／``No escaped character``）。
內建 seed ``20261015``：固定案例＋隨機組合（單雙引號、反斜線跳脫、引號內反斜線、尾端反斜線、空字串、全空白、非 ASCII），≥500 列。

用法::

    python3 scripts/rust/goldens/gen_shlex.py --out contracts/golden/shlex_split.tsv
"""

from __future__ import annotations

import argparse
import random
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _tsv import Row, write_tsv  # noqa: E402

SEED = 20261015
RANDOM_CASES = 560
SEP = "\x1f"

FIXED = [
    "", " ", "   ", "\t\r\n", "a", "a b", "a  b", " a b ", "a 'b c' \"d\\\"e\"", "x 'y", "x \\", "'", "\"", "\\",
    "''", "\"\"", "a ''", "a \"\"", "'' a", "a\\ b", "a\\\\b", "\"a\\\\b\"", "\"a\\b\"", "'a\\b'", "\"a\\\"b\"",
    "'a\"b'", "\"a'b\"", "a'b'c", "a\"b\"c", "\"a\nb\"", "\"a\\\nb\"", "a\\\nb", "#comment", "a #b", "中文 測試",
    "'中 文'", "\"é\"", "a\tb\rc\nd", "\\'", "\\\"", "'\\''", "\"\\$x\"", "a;b", "a|b", "a&&b", "$(x)", "`x`",
    "x=1 y", "\"unterminated \\\"", "'a' 'b", "\"a\" \"b", "end\\", "\"end\\", "'end\\'", "\x0b", "a\x0bb",
]

PIECES = ["a", "b", "c", " ", "  ", "\t", "\n", "'", "\"", "\\", "\\\\", "\\'", "\\\"", "'x y'", "\"x y\"", "\"\\\\\"",
          "''", "\"\"", "中", "é", "#", "=", "$", "-", "--", "\r", "z\\ z"]


def split_or_err(s: str) -> str:
    try:
        return "OK:" + SEP.join(shlex.split(s, posix=True))
    except ValueError as exc:
        return f"ERR:{exc}"


def build_rows() -> list[Row]:
    rng = random.Random(SEED)
    inputs = list(FIXED)
    for _ in range(RANDOM_CASES):
        inputs.append("".join(rng.choice(PIECES) for _ in range(rng.randint(0, 10))))
    return [(f"shx-{i:04d}", "shlex_split", s, split_or_err(s)) for i, s in enumerate(inputs)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 shlex_split.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_tsv(a.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
