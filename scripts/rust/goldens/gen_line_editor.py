#!/usr/bin/env python3
"""產生 ``contracts/golden/line_editor.tsv``（P6 Task 3；Python P1 ``UARTBridge._consume_console_input`` 為基準）。

以 ``object.__new__(UARTBridge)`` 取得未初始化實例，對
``ConsoleClient(client_id="g", label="g", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)``
逐 chunk 呼叫 ``_consume_console_input``（同一 case 共用同一 client，狀態跨 chunk 保留）。
內建 seed ``20261015``、1000 個隨機案例，並固定加入 SC-16 溢位案例（07 D-06）：恰 16385 bytes＋``\\r``、
16386 bytes＋``\\r``、16386 bytes＋backspace＋可列印字元＋``\\r\\n``、溢位行跨 chunk 結束、溢位後下一行正常、
16386 bytes＋``z\\x7f\\r``。

四欄 TSV（沿用 P3 ``_tsv.py`` 檔案慣例；``input``／``expected`` 欄為 ASCII 原字串，不經 hex(UTF-8) 包裝）：
``<case_id>\\tline_consume\\t<chunk hex,…>\\t<行數>:<line1 hex>+<line2 hex>…/<echo hex>,…``（行為 UTF-8 hex，可含 U+FFFD）。

用法::

    python3 scripts/rust/goldens/gen_line_editor.py --out contracts/golden/line_editor.tsv
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sw_core.uart_io import ConsoleClient, UARTBridge  # noqa: E402

SEED = 20261015
CASES = 1000
# 涵蓋：可列印、Tab、控制字元（緩衝不回顯）、backspace 兩種、CR／LF、UTF-8 多位元組與孤立 continuation、無效位元組。
ALPHABET = [b"a", b"b", b" ", b"\t", b"\x03", b"\x1b", b"\x08", b"\x7f", b"\r", b"\n", "中".encode(), b"\xe4", b"\xb8",
            b"\x80", b"\xff", b"\xc3\xa9", b"~", b"0"]


def _new_bridge() -> UARTBridge:
    return object.__new__(UARTBridge)


def _new_client() -> ConsoleClient:
    return ConsoleClient(client_id="g", label="g", master_fd=-1, slave_fd=-1, slave_path="", attached_at=0.0)


def run_case(chunks: list[bytes]) -> str:
    bridge = _new_bridge()
    client = _new_client()
    outs = []
    for c in chunks:
        lines, echo = bridge._consume_console_input(client, c)
        joined = "+".join(line.encode("utf-8").hex() for line in lines)
        outs.append(f"{len(lines)}:{joined}/{echo.hex()}")
    return ",".join(outs)


def split_chunks(rng: random.Random, data: bytes) -> list[bytes]:
    if not data:
        return []
    n = min(rng.randint(1, 6), len(data))
    cuts = sorted(rng.sample(range(1, len(data)), n - 1)) if n > 1 else []
    bounds = [0, *cuts, len(data)]
    return [data[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]


def fixed_cases() -> list[tuple[str, list[bytes]]]:
    a = b"a"
    return [
        ("le-ovf-exact-16385", [a * 16385 + b"\r"]),
        ("le-ovf-16386-cr", [a * 16386 + b"\r"]),
        ("le-ovf-16386-bs-print-crlf", [a * 16386 + b"\x7f\x7fbb\r\n"]),
        ("le-ovf-16386-z-bs-cr", [a * 16386 + b"z\x7f\r"]),
        ("le-ovf-span-chunks", [a * 16386, b"bb", b"\x7f", b"\r"]),
        ("le-ovf-then-normal", [a * 16390 + b"\r", b"ok\r"]),
        ("le-ovf-then-normal-same-chunk", [a * 16390 + b"\rok\r"]),
        ("le-ovf-lf-terminated", [a * 16386 + b"\n", b"\r"]),
        ("le-ovf-two-lines", [a * 16386 + b"\r" + a * 16386 + b"\r", b"x\r"]),
    ]


def build_rows() -> list[tuple[str, str, str, str]]:
    logger = logging.getLogger("serialwrap")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False  # 溢位 WARNING 不輸出到 stderr（golden 只比對位元組）
    rng = random.Random(SEED)
    rows = []
    for i in range(CASES):
        data = b"".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 48)))
        chunks = split_chunks(rng, data)
        rows.append((f"le-{i:04d}", "line_consume", ",".join(c.hex() for c in chunks), run_case(chunks)))
    for case_id, chunks in fixed_cases():
        rows.append((case_id, "line_consume", ",".join(c.hex() for c in chunks), run_case(chunks)))
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
    ap = argparse.ArgumentParser(description="產生 line_editor.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_raw_tsv(a.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
