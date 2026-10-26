#!/usr/bin/env python3
"""產生 ``contracts/golden/pyre_escape.tsv``（P7 Task 3 上游補件：``sw_proto::pyre::escape``；CPython 3.12 ``re.escape(str)`` 為基準）。

四欄 TSV（P3 ``_tsv.write_tsv``）：``<case_id>\\tre_escape\\t<hex(輸入 UTF-8)>\\t<hex(re.escape(輸入) UTF-8)>``。
案例：空字串、U+0000..U+007F 逐字元（含 NUL、全部 ASCII 標點與空白）、代表性非 ASCII（Latin-1、CJK、
組合字元、全形標點、U+0085／U+2028 等 Unicode 行界、BMP 外字元），以及內建 seed ``20261026`` 的隨機組合。
lone surrogate 無法以 UTF-8 寫出，不收。

用法::

    python3 scripts/rust/goldens/gen_pyre_escape.py --out contracts/golden/pyre_escape.tsv
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _tsv import Row, write_tsv  # noqa: E402

SEED = 20261026
RANDOM_CASES = 300

NON_ASCII = [
    "\x80", "\x85", "\xa0", "é", "ß", "ÿ", "Ā", "中", "文", "測試", "́", "é", "（", "）", "＊", "．",
    " ", " ", "　", "﻿", "￿", "😀", "\U0010ffff", "__READY__中文", "Ⅻ", "١٢٣",
]

FIXED = [
    "", "__READY__", "__READY__abc123", "echo __READY__${nonce}", "a.b", "a\\b", "(?m)^x$", "[a-z]+", "{1,2}",
    "a b\tc\nd\re\vf\fg", "--", "~/#&", "NUL\x00in", "\x00", "\x00\x00", "mixed 中文.*?", "abc_XYZ_019",
]

PIECES = [chr(c) for c in range(0x80)] + NON_ASCII + ["ab", "_", "0", "Z"]


def build_rows() -> list[Row]:
    inputs = list(FIXED)
    inputs += [chr(c) for c in range(0x80)]
    inputs += NON_ASCII
    rng = random.Random(SEED)
    for _ in range(RANDOM_CASES):
        inputs.append("".join(rng.choice(PIECES) for _ in range(rng.randint(0, 12))))
    return [(f"esc-{i:04d}", "re_escape", s, re.escape(s)) for i, s in enumerate(inputs)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 pyre_escape.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_tsv(a.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
