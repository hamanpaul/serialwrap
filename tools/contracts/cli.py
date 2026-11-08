"""契約目錄與驗證器命令列介面（`python3 -m tools.contracts`）。

提供 extract、check、summary、skill-section、effective 子命令。
輸出格式符合契約治理規範（DISK_STABLE JSON 序列化）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
import sys

from .catalog import EFFECTIVE_STATUS, load_catalogs
from .extract import extract, load_extraction_config
from .validate import CHECK_CODES, run_all, summarize


def _format_exc(exc: Exception) -> str:
    """將例外訊息正規化為單行（以分號分隔各行），符合契約治理 stderr 慣例。"""
    lines = str(exc).splitlines()
    return " ; ".join(lines) if lines else str(exc)


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口函式。"""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="contracts",
        description="serialwrap 契約目錄與驗證器工具",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # extract
    p_extract = subparsers.add_parser("extract", help="抽出程式碼實際輸出的碼")
    p_extract.add_argument(
        "--namespace",
        choices=["error_code", "session_last_error"],
        required=True,
        help="目標命名空間",
    )
    p_extract.add_argument(
        "--format",
        choices=["lines", "json"],
        default="lines",
        help="輸出格式（lines: 排序值清單；json: emission 物件陣列）",
    )
    p_extract.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo 根目錄（預設為當前目錄）",
    )

    # check
    p_check = subparsers.add_parser("check", help="執行全套契約檢查")
    p_check.add_argument(
        "--only",
        type=str,
        default=None,
        help="以逗號分隔之檢查代號清單（例：C02,C09）",
    )
    p_check.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo 根目錄（預設為當前目錄）",
    )

    # summary
    p_summary = subparsers.add_parser("summary", help="產出契約統計摘要")
    p_summary.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo 根目錄（預設為當前目錄）",
    )

    # skill-section
    p_skill = subparsers.add_parser("skill-section", help="產生 SKILL.md 受管區段內容")
    p_skill.add_argument(
        "--namespace",
        choices=["error_code", "session_last_error"],
        required=True,
        help="目標命名空間",
    )
    p_skill.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo 根目錄（預設為當前目錄）",
    )

    # effective
    p_effective = subparsers.add_parser("effective", help="列出生效之錯誤碼集合")
    p_effective.add_argument(
        "--namespace",
        choices=["error_code"],
        required=True,
        help="目標命名空間（目前僅支援 error_code）",
    )
    p_effective.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo 根目錄（預設為當前目錄）",
    )

    args = parser.parse_args(argv)
    repo_root = args.repo.resolve()

    if args.subcommand == "extract":
        try:
            cfg = load_extraction_config(repo_root / "contracts" / "code_extraction.yaml")
            res = extract(repo_root, cfg)
        except Exception as exc:
            print(f"contracts: {_format_exc(exc)}", file=sys.stderr)
            return 1

        if args.format == "lines":
            lines = sorted(res.values(args.namespace, include_passthrough=False))
            for line in lines:
                print(line)
        else:
            emissions = [
                dataclasses.asdict(e)
                for e in res.emissions
                if e.namespace == args.namespace
            ]
            print(json.dumps(emissions, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0

    elif args.subcommand == "check":
        if args.only:
            only_set = {c.strip() for c in args.only.split(",") if c.strip()}
            unknown = only_set - set(CHECK_CODES)
            if not only_set or unknown:
                unknown_str = ",".join(sorted(unknown)) if unknown else "empty"
                print(f"contracts: unknown check code(s): {unknown_str}", file=sys.stderr)
                return 1
        else:
            only_set = set()

        try:
            findings, s = run_all(repo_root)
        except Exception as exc:
            print(f"contracts: {_format_exc(exc)}", file=sys.stderr)
            return 1

        if only_set:
            findings = [f for f in findings if f.check in only_set or f.check == "C01"]

        ok = not any(f.severity == "FAIL" for f in findings)
        out = {
            "findings": [dataclasses.asdict(f) for f in findings],
            "ok": ok,
            "summary": s,
        }
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if ok else 1

    elif args.subcommand == "summary":
        try:
            cat = load_catalogs(repo_root)
            cfg = load_extraction_config(repo_root / "contracts" / "code_extraction.yaml")
            res = extract(repo_root, cfg)
            s = summarize(cat, res)
        except Exception as exc:
            print(f"contracts: {_format_exc(exc)}", file=sys.stderr)
            return 1

        print(json.dumps(s, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0

    elif args.subcommand == "skill-section":
        try:
            from .skill_section import render
        except ImportError as exc:
            print(f"contracts: skill_section 模組未實作 ({exc})", file=sys.stderr)
            return 1

        try:
            cat = load_catalogs(repo_root)
            text = render(cat, args.namespace)
            print(text)
            return 0
        except Exception as exc:
            print(f"contracts: {_format_exc(exc)}", file=sys.stderr)
            return 1

    elif args.subcommand == "effective":
        try:
            cat = load_catalogs(repo_root)
            cfg = load_extraction_config(repo_root / "contracts" / "code_extraction.yaml")
            res = extract(repo_root, cfg)
        except Exception as exc:
            print(f"contracts: {_format_exc(exc)}", file=sys.stderr)
            return 1

        emitted = res.values(args.namespace, include_passthrough=False)
        effective_catalog = {e.code for e in cat.error_codes if e.status in EFFECTIVE_STATUS}
        for code in sorted(emitted & effective_catalog):
            print(code)
        return 0

    return 0
