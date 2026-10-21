#!/usr/bin/env python3
"""產生 ``contracts/golden/pyjson.tsv``（P3-T3 的 Python oracle）。

oracle 為 CPython 標準函式庫 ``json``，基準版本 3.12.3（CI 釘版；其他版本輸出不保證相同）。
產生器只使用標準函式庫，不得 import ``sw_core``。輸出為確定性：固定 seed、固定 corpus、依序寫出。

用法::

    python3 scripts/rust/goldens/gen_pyjson.py --out contracts/golden/pyjson.tsv
"""

from __future__ import annotations

import argparse
import json
import random
import struct
import sys

from _tsv import write_tsv

BASELINE = (3, 12, 3)
FUZZ_SEED = 20261015
FUZZ_N = 3000
FLOAT_N = 10000

PROFILES = {
    "cli_stdout": dict(ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    "rpc_wire": dict(ensure_ascii=False, separators=(",", ":")),
    "rpc_early": dict(ensure_ascii=False),
    "disk_stable": dict(ensure_ascii=False, sort_keys=True, separators=(",", ":")),
}

CORPUS = [
    "{}", "[]", '{"b":1,"a":2}', '{"a":1,"a":2}', '{"k":"\\u00e9\\ud83d\\ude00"}', '"\\u2028/\\u007f"',
    "[1e16,1e-05,0.0001,-0.0,5.0,31.5,1e400,-1e400]", "NaN", "[Infinity,-Infinity]",
    "123456789012345678901234567890123456789", "-0", '{"x":[true,false,null]}',
    '"\\b\\f\\n\\r\\t\\"\\\\"', '"\\u0001"', " \t\n{\"a\" : 1}\r\n ",
    '"\x01"', '{"a":1,}', "[1 2]", '{"a" 1}', "{'a':1}", '"\\x"', '"\\u12"', "﻿{}", '{"a":1} x', "",
]

# fuzz 字串的 code point 範圍（排除 surrogate；控制字元 0x00–0x1f 亦納入）。
_SCALAR_RANGES = [
    (0x20, 0x7E),
    (0x80, 0x7FF),
    (0x800, 0xD7FF),
    (0xE000, 0xFFFF),
    (0x10000, 0x10FFFF),
    (0x00, 0x1F),
]
# 隨機替換／插入的字元集合（結構字元與跳脫相關字元）。
_MUT_CHARS = ' ,:[]{}"\\x0'


def _gen_str(rng: random.Random) -> str:
    n = rng.randint(0, 8)
    chars = []
    for _ in range(n):
        lo, hi = rng.choice(_SCALAR_RANGES)
        chars.append(chr(rng.randint(lo, hi)))
    return "".join(chars)


def _gen_float(rng: random.Random) -> float:
    r = rng.random()
    if r < 0.1:
        return rng.choice([1e308, -1e308, 1e-308, 5e-324, 1e16, 1e-5, 1e-4, -0.0, 31.5, 1.7976931348623157e308])
    if r < 0.2:
        return rng.uniform(-1e308, 1e308)
    return rng.uniform(-1e6, 1e6)


def _gen_scalar(rng: random.Random):
    kind = rng.choices(["null", "bool", "int", "float", "str"], weights=[1, 2, 3, 3, 3])[0]
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "int":
        return rng.randint(-(2**70) + 1, 2**70 - 1)
    if kind == "float":
        return _gen_float(rng)
    return _gen_str(rng)


def gen_value(rng: random.Random, depth: int = 0):
    """遞迴產生隨機 JSON 值；depth≥6 時只產生 scalar。"""
    if depth >= 6 or rng.random() < 0.6:
        return _gen_scalar(rng)
    if rng.random() < 0.5:
        return [gen_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {_gen_str(rng): gen_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def _mutate(rng: random.Random, s: str) -> str:
    op = rng.choice(["del", "ins", "rep"])
    if op == "del" and s:
        i = rng.randrange(len(s))
        return s[:i] + s[i + 1 :]
    if op == "rep" and s:
        i = rng.randrange(len(s))
        return s[:i] + rng.choice(_MUT_CHARS) + s[i + 1 :]
    i = rng.randrange(len(s) + 1)
    return s[:i] + rng.choice(_MUT_CHARS) + s[i:]


def fuzz(seed: int, n: int) -> list[str]:
    """回傳 n 筆確定性的 JSON 文字（約 10% 會被再做一次字元級突變，以產生錯誤輸入）。"""
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(n):
        v = gen_value(rng, 0)
        text = json.dumps(v, ensure_ascii=rng.random() < 0.5)
        if rng.random() < 0.10:
            text = _mutate(rng, text)
        out.append(text)
    return out


def has_lone_surrogate(v) -> bool:
    """遞迴檢查（含 dict key）是否含 U+D800–U+DFFF。"""
    if isinstance(v, str):
        return any(0xD800 <= ord(c) <= 0xDFFF for c in v)
    if isinstance(v, list):
        return any(has_lone_surrogate(x) for x in v)
    if isinstance(v, dict):
        return any(has_lone_surrogate(k) or has_lone_surrogate(x) for k, x in v.items())
    return False


def build_rows() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for i, text in enumerate(CORPUS + fuzz(FUZZ_SEED, FUZZ_N)):
        try:
            v = json.loads(text)
        except json.JSONDecodeError as e:
            rows.append((f"j{i}", "loads_error", text, str(e)))
            continue
        except RecursionError:
            rows.append((f"j{i}", "loads_error", text, "RECURSION"))
            continue
        if has_lone_surrogate(v):
            continue  # RPC-H-03：非契約差異，由 Rust 單元測試驗 U+FFFD
        for name, kw in PROFILES.items():
            rows.append((f"j{i}", f"dumps:{name}", text, json.dumps(v, **kw)))

    rng = random.Random(FUZZ_SEED)
    for i in range(FLOAT_N):
        bits = rng.getrandbits(64)
        x = struct.unpack("<d", struct.pack("<Q", bits))[0]
        rows.append((f"f{i}", "float_repr", f"{bits:016x}", repr(x)))

    # 深度：兩平台（Linux 3.12.3 ≈9998、Windows 3.12.10 ≈2998）都必須成功的區段。
    for depth in (100, 500, 1000, 2000, 2500):
        text = "[" * depth + "]" * depth
        v = json.loads(text)  # 失敗即產生器 raise，不寫入 golden
        rows.append((f"d{depth}", "dumps:rpc_wire", text, json.dumps(v, **PROFILES["rpc_wire"])))

    # 深度：兩平台都必須 RecursionError 的區段。
    for depth in (12000, 20000):
        text = "[" * depth + "]" * depth
        try:
            json.loads(text)
        except RecursionError:
            rows.append((f"d{depth}", "loads_error", text, "RECURSION"))
        else:
            raise SystemExit(f"depth {depth} 未觸發 RecursionError，golden 前提不成立")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 pyjson golden TSV（CPython json 為 oracle）")
    ap.add_argument("--out", required=True, help="輸出 TSV 路徑")
    args = ap.parse_args(argv)
    if sys.version_info[:3] != BASELINE:
        print(
            f"warning: golden 基準為 CPython {'.'.join(map(str, BASELINE))}，"
            f"目前 {sys.version.split()[0]}；輸出可能與 contracts/golden 不同",
            file=sys.stderr,
        )
    write_tsv(args.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
