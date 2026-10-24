#!/usr/bin/env python3
"""產生 ``contracts/golden/telnet_filter.tsv``（P6 Task 2；Python ``sw_core.telnet_console.TelnetFilter`` 為基準）。

固定 seed ``20261015``、2000 案：以 ``random.Random(seed)`` 從 PICK 集合（涵蓋所有狀態轉移）產生長度 0～96 的序列，
隨機切成 1～8 個非空 chunk（長度 0 者為 0 個 chunk），逐 chunk 餵入同一個 ``TelnetFilter``。

四欄 TSV（沿用 P3 ``_tsv.py`` 的檔案慣例：UTF-8、LF、無註解、依產生順序、``(case_id, op)`` 唯一）：
``<case_id>\\ttelnet_feed\\t<chunk hex,…>\\t<uart hex>/<reply hex>,…``。
本 golden 的 bytes 欄位不經 UTF-8 解碼，``input``／``expected`` 欄即為上述 ASCII hex 串本身（07 Task 2 Interfaces；
Rust 端 ``crates/sw-core/tests/common/golden.rs`` 保留原字串、不做 hex 解碼），因此不經 ``_tsv.write_tsv`` 的
hex(UTF-8) 包裝，只沿用其格式與唯一性規則。

用法::

    python3 scripts/rust/goldens/gen_telnet_filter.py --out contracts/golden/telnet_filter.tsv
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw_core.telnet_console import TelnetFilter  # noqa: E402

SEED = 20261015
CASES = 2000
IAC, WILL, WONT, DO, DONT, SB, SE = 0xFF, 0xFB, 0xFC, 0xFD, 0xFE, 0xFA, 0xF0
BIN, ECHO, SGA, TTYPE, NAWS = 0x00, 0x01, 0x03, 0x18, 0x1F
# 與 crates/sw-core/tests/telnet_filter.rs 的 PICK 相同（涵蓋所有狀態轉移）。
PICK = [IAC, WILL, WONT, DO, DONT, SB, SE, 0x00, 0x0A, 0x0D, ECHO, SGA, TTYPE, NAWS, 0xF1, 0xF6, ord("a")]


def split_chunks(rng: random.Random, data: bytes) -> list[bytes]:
    if not data:
        return []
    n = min(rng.randint(1, 8), len(data))
    cuts = sorted(rng.sample(range(1, len(data)), n - 1)) if n > 1 else []
    bounds = [0, *cuts, len(data)]
    return [data[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]


def build_rows() -> list[tuple[str, str, str, str]]:
    rng = random.Random(SEED)
    rows = []
    for i in range(CASES):
        data = bytes(rng.choice(PICK) for _ in range(rng.randint(0, 96)))
        chunks = split_chunks(rng, data)
        t = TelnetFilter()
        outs = []
        for c in chunks:
            uart, reply = t.feed(c)
            outs.append(f"{uart.hex()}/{reply.hex()}")
        rows.append((f"tf-{i:04d}", "telnet_feed", ",".join(c.hex() for c in chunks), ",".join(outs)))
    return rows


def write_raw_tsv(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    seen: set[tuple[str, str]] = set()
    for case_id, op, _i, _e in rows:
        if (case_id, op) in seen:
            raise ValueError(f"golden 列重複：case_id={case_id!r} op={op!r}")
        seen.add((case_id, op))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write("\t".join(row) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 telnet_filter.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_raw_tsv(a.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
