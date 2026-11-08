#!/usr/bin/env python3
"""產生 ``contracts/golden/unicode_tables.txt``（P3-T4 的 Unicode 分類表 oracle）。

內容為 CPython 3.12.3 ``unicodedata``（Unicode 15.0.0）對**全部碼點**（``0..=0x10FFFF``）的四組判定結果，
以閉區間列出（``LO-HI``；單點為 ``LO``）：

- ``space``：``str.isspace``（``py_is_space`` 與之逐碼點比對）
- ``decimal_zero``：``unicodedata.decimal(c) == 0`` 的碼點（每個零點 ``z`` 對應 ``z..=z+9``，本產生器驗證）
- ``digit``：``str.isdigit``（``py_isdigit`` 的 ``DIGIT_RANGES`` 來源）
- ``nonprint``：``not str.isprintable``（``py_isprintable`` 的 ``NONPRINT_RANGES`` 來源；U+0020 不在其中）

``crates/sw-proto/src/pycompat.rs`` 內嵌的表與此檔逐項比對（單元測試），此檔由本產生器重跑比對（``test_rust_goldens_fresh.py``）。
產生器只使用標準函式庫，不得 import ``sw_core``。輸出為確定性：依碼點升冪、LF、UTF-8。

用法::

    python3 scripts/rust/goldens/gen_unicode_tables.py --out contracts/golden/unicode_tables.txt
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import Callable

MAX_CP = 0x110000
HEADER = (
    "# Unicode 15.0.0 分類表（CPython 3.12.3 unicodedata 產生；勿手改）。",
    "# 產生器：scripts/rust/goldens/gen_unicode_tables.py；格式：每行 <key> <token>...，token 為 0xHHHHH 或 0xHHHHH-0xHHHHH（閉區間）。",
)


def _ranges(pred: Callable[[str], bool]) -> list[tuple[int, int]]:
    """全部碼點中滿足 ``pred`` 的閉區間（升冪、極大化）。"""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for cp in range(MAX_CP):
        hit = pred(chr(cp))
        if hit and start is None:
            start = cp
        elif not hit and start is not None:
            out.append((start, cp - 1))
            start = None
    if start is not None:
        out.append((start, MAX_CP - 1))
    return out


def _decimal_zeros() -> list[int]:
    """Nd 零點；同時驗證每個零點之後 9 個碼點的 decimal 值依序為 1..9，且 decimal 有定義的碼點恰為這些區間。"""
    zeros = [cp for cp in range(MAX_CP) if unicodedata.decimal(chr(cp), None) == 0]
    defined: set[int] = set()
    for z in zeros:
        for k in range(10):
            cp = z + k
            if unicodedata.decimal(chr(cp), None) != k:
                raise ValueError(f"Nd 區段不連續：U+{cp:04X} 的 decimal 應為 {k}")
            defined.add(cp)
    actual = {cp for cp in range(MAX_CP) if unicodedata.decimal(chr(cp), None) is not None}
    if actual != defined:
        raise ValueError("decimal 定義的碼點與 Nd 零點區段不一致")
    return zeros


def _fmt_token(lo: int, hi: int) -> str:
    return f"0x{lo:05X}" if lo == hi else f"0x{lo:05X}-0x{hi:05X}"


def build_lines() -> list[str]:
    lines = list(HEADER)
    lines.append("space " + " ".join(_fmt_token(lo, hi) for lo, hi in _ranges(str.isspace)))
    lines.append("decimal_zero " + " ".join(_fmt_token(z, z) for z in _decimal_zeros()))
    lines.append("digit " + " ".join(_fmt_token(lo, hi) for lo, hi in _ranges(str.isdigit)))
    lines.append(
        "nonprint "
        + " ".join(_fmt_token(lo, hi) for lo, hi in _ranges(lambda c: not c.isprintable()))
    )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="輸出檔（例如 contracts/golden/unicode_tables.txt）")
    args = parser.parse_args()
    text = "\n".join(build_lines()) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"產生 {args.out} 完成（Python {sys.version.split()[0]}）")


if __name__ == "__main__":
    main()
