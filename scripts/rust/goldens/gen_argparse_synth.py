#!/usr/bin/env python3
"""產生 argparse 合成案例 golden TSV（P3-T10）。

依 plan §2 Task 10 規格：
建 12 個合成 parser（涵蓋 nargs='*'、append、count、必填 mutex、choices、
type=int 負數、allow_abbrev=False、長選項前綴衝突、-xVALUE、-x=VALUE、合併旗標 -vv），
各跑 10 組 argv；以 Python 3.12.3 argparse 產出 expected 結果並寫入
contracts/golden/argparse_synth.tsv。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

# 嚴格鎖定 Python 3.12.3 方言
if sys.version_info[:3] != (3, 12, 3):
    sys.stderr.write(f"錯誤：Python 直譯器版本 {sys.version.split()[0]} ≠ 3.12.3，無法對齊 argparse 方言\n")
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tsv import write_tsv  # noqa: E402


def build_parsers() -> list[tuple[str, argparse.ArgumentParser, list[list[str]]]]:
    parsers = []

    # P01: nargs='*'
    p01 = argparse.ArgumentParser(prog="p01")
    p01.add_argument("--items", nargs="*", default=[])
    p01.add_argument("-R", action="store_true", default=False)
    p01.add_argument("words", nargs="*")
    cases01 = [
        [],
        ["--items"],
        ["--items", "a"],
        ["--items", "a", "b"],
        ["--items=v", "x"],
        ["--items", "--", "x"],
        ["x", "-R", "y"],
        ["x", "y"],
        ["--unknown"],
        ["extra"],
    ]
    parsers.append(("P01", p01, cases01))

    # P02: action='append'
    p02 = argparse.ArgumentParser(prog="p02")
    p02.add_argument("-i", "--item", action="append", default=None)
    cases02 = [
        [],
        ["--item", "a"],
        ["-i", "a"],
        ["-ia"],
        ["-i=a"],
        ["--item=a", "--item=b"],
        ["-ia", "-ib"],
        ["--item"],
        ["--item", "a", "-i", "b", "-i", "c"],
        ["--item", "-v foo"],
    ]
    parsers.append(("P02", p02, cases02))

    # P03: action='count'
    p03 = argparse.ArgumentParser(prog="p03")
    p03.add_argument("-v", "--verbose", action="count", default=0)
    cases03 = [
        [],
        ["-v"],
        ["-vv"],
        ["-vvv"],
        ["--verbose"],
        ["--verbose", "--verbose"],
        ["-v", "--verbose"],
        ["-v", "-v", "-v"],
        ["--verbose=1"],
        ["extra"],
    ]
    parsers.append(("P03", p03, cases03))

    # P04: 必填 mutex
    p04 = argparse.ArgumentParser(prog="p04")
    g04 = p04.add_mutually_exclusive_group(required=True)
    g04.add_argument("--foo", default=None)
    g04.add_argument("--bar", default=None)
    cases04 = [
        [],
        ["--foo", "1"],
        ["--bar", "2"],
        ["--foo=1"],
        ["--bar=2"],
        ["--foo", "1", "--bar", "2"],
        ["--foo"],
        ["--foo", "1", "--foo", "2"],
        ["--unknown"],
        ["extra"],
    ]
    parsers.append(("P04", p04, cases04))

    # P05: choices
    p05 = argparse.ArgumentParser(prog="p05")
    p05.add_argument("--mode", choices=["auto", "manual", "off"], default="auto")
    cases05 = [
        [],
        ["--mode", "auto"],
        ["--mode", "manual"],
        ["--mode", "off"],
        ["--mode=auto"],
        ["--mode", "bad"],
        ["--mode=bad"],
        ["--mode"],
        ["--mode", "AUTO"],
        ["extra"],
    ]
    parsers.append(("P05", p05, cases05))

    # P06: type=int 負數
    p06 = argparse.ArgumentParser(prog="p06")
    p06.add_argument("-n", "--num", type=int, default=0)
    cases06 = [
        [],
        ["--num", "-٣"],
        ["--num", "-1"],
        ["-n", "-5"],
        ["-n-3"],
        ["--num=-10"],
        ["--num", "-1."],
        ["--num", "abc"],
        ["--num", "--", "-1"],
        ["--num"],
    ]
    parsers.append(("P06", p06, cases06))

    # P07: allow_abbrev=False
    p07 = argparse.ArgumentParser(prog="p07", allow_abbrev=False)
    p07.add_argument("--foobar", default=None)
    p07.add_argument("--foobaz", default=None)
    cases07 = [
        [],
        ["--foobar", "1"],
        ["--foobaz", "2"],
        ["--foo", "1"],
        ["--foob", "1"],
        ["--fooba", "1"],
        ["--foobar=1"],
        ["--foobaz=2"],
        ["--foobar"],
        ["extra"],
    ]
    parsers.append(("P07", p07, cases07))

    # P08: 長選項前綴衝突
    p08 = argparse.ArgumentParser(prog="p08")
    p08.add_argument("--version", action="store_true", default=False)
    p08.add_argument("--verbose", action="store_true", default=False)
    cases08 = [
        [],
        ["--version"],
        ["--verbose"],
        ["--v"],
        ["--ver"],
        ["--verb"],
        ["--version=1"],
        ["--help", "--ver"],
        ["--unknown"],
        ["extra"],
    ]
    parsers.append(("P08", p08, cases08))

    # P09: -xVALUE（短選項直接相連值）
    p09 = argparse.ArgumentParser(prog="p09")
    p09.add_argument("-f", default=None)
    p09.add_argument("-b", action="store_true", default=False)
    cases09 = [
        [],
        ["-fVALUE"],
        ["-f", "VALUE"],
        ["-b"],
        ["-bfVALUE"],
        ["-f"],
        ["-f="],
        ["-f=VAL"],
        ["-b", "-fVAL"],
        ["extra"],
    ]
    parsers.append(("P09", p09, cases09))

    # P10: -x=VALUE（短選項以等號連接值）
    p10 = argparse.ArgumentParser(prog="p10")
    p10.add_argument("-s", default=None)
    cases10 = [
        [],
        ["-s=VAL"],
        ["-s=123"],
        ["-s="],
        ["-s", "VAL"],
        ["-s", "=VAL"],
        ["-s=-"],
        ["-s"],
        ["-s=a=b"],
        ["extra"],
    ]
    parsers.append(("P10", p10, cases10))

    # P11: 合併旗標 -vv 與 boolean flags
    p11 = argparse.ArgumentParser(prog="p11")
    p11.add_argument("-a", action="store_true", default=False)
    p11.add_argument("-b", action="store_true", default=False)
    p11.add_argument("-c", action="store_true", default=False)
    p11.add_argument("-v", action="count", default=0)
    cases11 = [
        [],
        ["-a"],
        ["-ab"],
        ["-abc"],
        ["-vv"],
        ["-abvv"],
        ["-vavb"],
        ["-ax"],
        ["-a", "-b"],
        ["extra"],
    ]
    parsers.append(("P11", p11, cases11))

    # P12: 綜合混合型
    p12 = argparse.ArgumentParser(prog="p12")
    p12.add_argument("--name", required=True)
    p12.add_argument("-v", action="count", default=0)
    p12.add_argument("--count", type=int, default=1)
    p12.add_argument("-f", action="store_true", default=False)
    cases12 = [
        [],
        ["--name", "test"],
        ["--name=test", "-vf"],
        ["-vfv", "--name", "x"],
        ["--name", "x", "--count", "5"],
        ["--name", "x", "--count", "-2"],
        ["--name", "x", "--count", "bad"],
        ["--name"],
        ["-v", "-f"],
        ["extra"],
    ]
    parsers.append(("P12", p12, cases12))

    return parsers


def run_parser_case(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    # 固定 COLUMNS=78
    old_columns = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "78"

    stdout_io = io.StringIO()
    stderr_io = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_io, stderr_io

    try:
        args = parser.parse_args(argv)
        vals = vars(args)
        res = {
            "ok": True,
            "rc": 0,
            "stdout": stdout_io.getvalue(),
            "stderr": stderr_io.getvalue(),
            "vals": vals,
        }
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        res = {
            "ok": False,
            "rc": rc,
            "stdout": stdout_io.getvalue(),
            "stderr": stderr_io.getvalue(),
        }
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        if old_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = old_columns

    return json.dumps(res, sort_keys=True, ensure_ascii=False)


def main() -> None:
    tsv_path = REPO_ROOT / "contracts" / "golden" / "argparse_synth.tsv"
    tsv_path.parent.mkdir(parents=True, exist_ok=True)

    parsers = build_parsers()
    rows = []

    for pid, parser, cases in parsers:
        assert len(cases) == 10, f"{pid} 案例數不等於 10"
        for idx, argv in enumerate(cases):
            op = f"case_{idx:02d}"
            input_json = json.dumps(argv, ensure_ascii=False)
            expected_json = run_parser_case(parser, argv)
            rows.append((pid, op, input_json, expected_json))

    write_tsv(tsv_path, rows)
    print(f"已產出 {len(rows)} 筆合成案例至 {tsv_path}")


if __name__ == "__main__":
    main()
