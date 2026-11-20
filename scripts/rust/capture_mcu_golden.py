#!/usr/bin/env python3
"""以 Python `sw_core.mcu_patterns` 產生 MCU pattern golden（P8-T2）。

輸出 `contracts/golden/mcu_patterns.ndjson`，每行 `{"fn", "in", "out"}`：
- `out` 為成功時的字串（或 `rpc` 的列表）；失敗時為 `{"error": "<Python 例外類別名>"}`。
- Rust 測試 `crates/sw-core/tests/mcu_patterns_golden.rs` 只讀此檔，不呼叫 Python。

用法：
  python3 scripts/rust/capture_mcu_golden.py            # 寫入 contracts/golden
  python3 scripts/rust/capture_mcu_golden.py --stdout   # 輸出到 stdout（新鮮度測試用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sw_core.mcu_patterns import McuPattern, McuPatternRegistry, _hex_to_bytes  # noqa: E402

OUTPUT = REPO_ROOT / "contracts" / "golden" / "mcu_patterns.ndjson"

HEX_CASES: list[str] = [
    "5555",
    "55 55",
    "0x55 0x55",
    "0X55",
    "55\t55",
    "5",
    "zz",
    "",
    "00cc",
    "0x0x55",
    "55\n55",
    "5 5",
    "\t55",
    "55\t",
    "０５",
    "0x",
    "x55",
    "55\r\n66",
]

_OK_DICT: dict[str, Any] = {
    "family": "f",
    "probe": "5555",
    "expect": "00cc",
    "non_destructive": True,
}


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in _OK_DICT.items() if k != key}


def _with(**kw: Any) -> dict[str, Any]:
    return {**_OK_DICT, **kw}


DICT_CASES: list[dict[str, Any]] = [
    _OK_DICT,
    {"family": "f", "probe": "55 55", "expect": "0x00 0xcc", "baud": 9600,
     "timeout_ms": 1500, "non_destructive": True},
    _with(baud="9600"),
    _with(baud="x"),
    _with(baud=9600.7),
    _with(baud=True),
    _with(baud=None),
    _with(timeout_ms=0),
    _with(timeout_ms="abc"),
    _without("non_destructive"),
    _with(non_destructive="false"),
    _with(non_destructive=0),
    _with(non_destructive=""),
    _with(non_destructive=False),
    _without("family"),
    _with(family=123),
    _with(family=True),
    _with(probe=5555),
    _with(probe=None),
    _with(probe=["55"]),
    _with(probe="5"),
    _without("expect"),
    {"probe": "zz", "non_destructive": False},
    {"family": "f", "probe": "zz", "expect": "00", "baud": None, "non_destructive": True},
    {"family": "f", "probe": "55", "expect": "00", "baud": None},
]

_DEF_ROW: dict[str, Any] = {
    "family": "ti-cc26xx", "probe": "5555", "expect": "00cc",
    "baud": 9600, "non_destructive": True,
}

LOAD_CASES: list[Any] = [
    None,
    [],
    [_DEF_ROW],
    [{"family": "x-new", "probe": "aa", "expect": "bb", "baud": 57600, "non_destructive": True}],
    [
        {"family": "x", "probe": "aa", "expect": "bb", "non_destructive": True},
        {"family": "x", "probe": "cc", "expect": "dd", "baud": 1, "timeout_ms": 9,
         "non_destructive": True},
    ],
    [{"family": "x", "probe": "aa", "expect": "bb", "non_destructive": False}],
    [{"family": "y", "probe": "zz", "expect": "bb", "non_destructive": True}],
    [
        {"family": "ti-cc26xx", "probe": "55", "expect": "00", "non_destructive": True},
        {"family": "a", "probe": "01", "expect": "02", "non_destructive": True},
    ],
]

RPC_CASES: list[Any] = [
    None,
    [{"family": "x-new", "probe": "0x0a", "expect": "0b", "baud": 57600,
      "non_destructive": True}],
]


def _pattern_str(p: McuPattern) -> str:
    return f"{p.family}|{p.probe.hex(' ')}|{p.expect.hex(' ')}|{p.baud}|{p.timeout_ms}"


def _run(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - golden 記錄例外類別名
        return {"error": type(exc).__name__}


def build_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for s in HEX_CASES:
        out = _run(lambda s=s: _hex_to_bytes(s).hex(" "))
        records.append({"fn": "hex_to_bytes", "in": s, "out": out})
    for d in DICT_CASES:
        out = _run(lambda d=d: _pattern_str(McuPattern.from_dict(d)))
        records.append({"fn": "from_dict", "in": d, "out": out})
    for rows in LOAD_CASES:
        out = _run(lambda rows=rows: ",".join(
            _pattern_str(p) for p in McuPatternRegistry.load(rows).all()))
        records.append({"fn": "load", "in": rows, "out": out})
    for rows in RPC_CASES:
        out = _run(lambda rows=rows: [
            {"family": p.family, "probe": p.probe.hex(" "), "expect": p.expect.hex(" "),
             "baud": p.baud}
            for p in McuPatternRegistry.load(rows).all()])
        records.append({"fn": "rpc", "in": rows, "out": out})
    return records


def render() -> str:
    lines = [
        json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        for rec in build_records()
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stdout", action="store_true", help="輸出到 stdout 而非寫檔")
    args = parser.parse_args(argv)
    data = render().encode("utf-8")
    if args.stdout:
        sys.stdout.buffer.write(data)
    else:
        OUTPUT.write_bytes(data)
        print(f"wrote {OUTPUT} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
