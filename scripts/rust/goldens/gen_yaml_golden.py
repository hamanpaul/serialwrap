#!/usr/bin/env python3
"""產生 ``crates/sw-proto/tests/golden/yaml_emit.ndjson``（P4-T17 的 Python oracle：PyYAML ``safe_dump``）。

oracle 為 ``yaml.safe_dump(value, allow_unicode=..., sort_keys=True, default_flow_style=False)``，
基準版本 PyYAML 6.0.3（其他版本輸出不保證相同，會發出警告）。產生器只使用 PyYAML 與標準函式庫，
不得 import ``sw_core``。輸出為確定性：固定語料、依序寫出。

每列為一個 JSON 物件（``ensure_ascii=False``、緊湊分隔、UTF-8、LF 結尾）::

    {"input": <JSON 值>, "allow_unicode": <bool>, "out": "<safe_dump 輸出>"}

``input`` 只含 JSON 可表示的值（null、bool、整數、有限浮點、字串、陣列、物件）；非有限浮點與孤立代理字元
不入語料（``.inf``／``.nan`` 等 YAML 字樣以字串涵蓋）。``sort_keys`` 固定為 true，對應 ``config.yaml`` 的
Rust 呼叫方式 ``safe_dump(allow_unicode, sort_keys=true)``。

用法::

    python3 scripts/rust/goldens/gen_yaml_golden.py            # 寫出 golden
    python3 scripts/rust/goldens/gen_yaml_golden.py --check    # 比對既有 golden 與重新產生的內容（不一致 exit 1）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parent
REPO = _DIR.parents[2]
GOLDEN = REPO / "crates" / "sw-proto" / "tests" / "golden" / "yaml_emit.ndjson"
BASELINE_PYYAML = "6.0.3"
MIN_ROWS = 150

# 純量語料：YAML 1.1 bool／null 字樣、數字與浮點字樣、timestamp、空白與換行、指示字元、控制與非可印字元、
# CJK／非 BMP／BOM、Windows 路徑、serialwrap 常見值、長字串（80 欄折行）與非字串純量。
SCALARS: tuple = (
    # YAML 1.1 bool／null 字樣
    "y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO",
    "true", "True", "TRUE", "false", "False", "FALSE",
    "on", "On", "ON", "off", "Off", "OFF", "~", "null", "Null", "NULL", "",
    # 數字與特殊浮點字樣
    "1", "-1", "+1", "0", "007", "0o17", "017", "0x1F", "0b1010", "1_000", "1:20",
    "1.0", "1e3", "1.5e+3", ".5", "-.5", ".inf", "-.inf", ".Inf", ".NaN", ".nan", "=", "<<", "0.", "1,000",
    # timestamp 字樣
    "2001-12-14", "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "2001-12-15 2:59:43.10",
    # 前後空白、換行、Tab、特殊空白
    " a", "a ", " ", "\ta", "a\tb", "a\nb", "a\n", "\n", "line1\nline2\n", "a\r\nb",
    "a\u0085b", "\u2028", "\u00a0x", "  leading and trailing  ",
    # 冒號、井字號、引號
    "a: b", "a:b", "a #b", "a#b", "#a", "a: ", "key:", "http://x", "tcp://127.0.0.1:48700",
    "C:\\Users\\a", "\\\\server\\share", "a'b", 'a"b', "'quoted'", '"dq"',
    # 指示字元開頭（-?:,[]{}#&*!|>'"%@ 與反引號）
    "-a", "- a", "?a", "? a", ":a", ",a", "[a", "]a", "{a", "}a", "&a", "*a", "!a", "|a", ">a",
    "'a", "%a", "@a", "`a", "-", "?", ":",
    # 控制字元與非可印字元
    "\x01", "a\x7fb", "\x1b[0m", "\x00", "\x08", "\x0c", "\x7f",
    # Unicode（CJK、非 BMP、BOM、零寬）
    "中文", "繁體 字串", "日本語", "é", "\U0001F600", "a\U0001F600b", "\ufeff", "\u200b",
    # serialwrap 常見值
    "COM3", "COM10", "/dev/serial/by-id/usb-FTDI_x-if00-port0", "~/.config/serialwrap", "${HOME}", "windows",
    # 長字串（超過 80 欄者由 PyYAML 折行）
    "lorem ipsum dolor sit amet " * 6, "x" * 120, "中文" * 40,
    # 非字串純量
    0, -1, 42, 9007199254740993, 10 ** 30, True, False, None,
    0.0, 1.0, 0.1, 1e16, 1.5e-7, 123456789.125, -2.5,
)

# 結構語料：巢狀、空容器、key 排序、key 需引號、Windows 設定形狀、浮點與大整數、長行與多行字串。
STRUCTURAL: tuple = (
    {"windows": {"exclude_coms": ["COM3", "COM10"]}},
    {"tcp": "tcp://127.0.0.1:48700", "path": "C:\\Users\\a"},
    {"z": 1, "a": 2, "m": {"y": 1, "b": 2}},
    {"b": [1, 2.5, True, None, "x"], "a": {}},
    [],
    {},
    [[], {}, [1, [2, [3]]]],
    {"a": {"b": {"c": [1, {"d": None}]}}},
    {"a: b": 1, "": 2, "1": 3, "true": 4, "null": 5, "#k": 6, "-k": 7, "中文": "值"},
    [[{"a": 1}]],
    [[{"a": 1, "b": 2}, 3]],
    [[[1, 2]]],
    {"k": [[{"a": 1}, 2], 3]},
    [["multi\nline"]],
    [["word " * 20 + "end"]],
    {"a\nb": 1},
    {"x " * 48: 1},
    {"x" * 123: 1},
    {"x" * 128: 1},
    {"": [1, 2]},
    {"unicode": "\t\u00a0"},
    {"unicode": "\t😀"},
    {"f": [1.0, 0.1, 1e16, 1.5e-7, -2.5, 123456789.125, -0.0]},
    {"i": [0, -1, 9007199254740993, 10 ** 30, -(10 ** 30)]},
    {"long": ["word " * 20 + "end", "x" * 100]},
    {"lines": "first\nsecond\n\nthird", "crlf": "a\r\nb", "tab": "a\tb"},
)

# P4-T6 runtime config 形狀（`RuntimeConfig::to_yaml` 以 allow_unicode=True 寫回）：空 dict、只有 mode、
# mode＋tcp socket、windows.exclude_coms、中文值、需引號的 "on"；附加於語料尾端且只產 allow_unicode=True 列。
RUNTIME_CONFIG: tuple = (
    {},
    {"supervision_mode": "on-demand"},
    {"socket_path": "tcp://127.0.0.1:48700", "supervision_mode": "windows-task"},
    {"windows": {"exclude_coms": ["COM3", "COM10"]}},
    {"greeting": "你好世界", "supervision_mode": "systemd-user"},
    {"supervision_mode": "on-demand", "tag": "on"},
)


def build_rows() -> list[tuple[object, bool]]:
    """回傳 ``(input, allow_unicode)`` 清單；順序固定。"""
    rows: list[tuple[object, bool]] = []
    for allow_unicode in (True, False):
        for value in SCALARS:
            rows.append((value, allow_unicode))
            rows.append(({"k": value, "n": [value]}, allow_unicode))
        for value in STRUCTURAL:
            rows.append((value, allow_unicode))
    for value in RUNTIME_CONFIG:
        rows.append((value, True))
    return rows


def render_lines() -> list[str]:
    lines: list[str] = []
    for value, allow_unicode in build_rows():
        out = yaml.safe_dump(value, allow_unicode=allow_unicode, sort_keys=True, default_flow_style=False)
        record = {"input": value, "allow_unicode": allow_unicode, "out": out}
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return lines


def render() -> bytes:
    return ("\n".join(render_lines()) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 yaml_emit golden（PyYAML safe_dump 為 oracle）")
    ap.add_argument("--check", action="store_true", help="比對既有 golden 與重新產生的內容，不一致則 exit 1")
    ap.add_argument("--out", default=str(GOLDEN), help="輸出路徑（預設 crates/sw-proto/tests/golden/yaml_emit.ndjson）")
    args = ap.parse_args(argv)
    if yaml.__version__ != BASELINE_PYYAML:
        print(
            f"warning: golden 基準為 PyYAML {BASELINE_PYYAML}，目前 {yaml.__version__}；輸出可能與 golden 不同",
            file=sys.stderr,
        )
    content = render()
    row_count = content.count(b"\n")
    if row_count < MIN_ROWS:
        print(f"yaml_emit golden 列數 {row_count} < 下限 {MIN_ROWS}", file=sys.stderr)
        return 1
    target = Path(args.out)
    if args.check:
        if not target.is_file():
            print(f"golden 不存在：{target}", file=sys.stderr)
            return 2
        existing = target.read_bytes()
        if existing != content:
            want = content.split(b"\n")
            got = existing.split(b"\n")
            first = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
            print(
                f"golden 與重新產生的內容不一致：第 {first + 1} 列起不同（既有 {len(got)} 行、重產 {len(want)} 行）",
                file=sys.stderr,
            )
            return 1
        print(f"yaml_emit golden 一致：{row_count} 列", file=sys.stderr)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print(f"yaml_emit golden：{row_count} 列 → {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
