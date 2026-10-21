#!/usr/bin/env python3
"""產生 ``contracts/golden/load_profiles.tsv``（P3-T6 的 Python oracle：``sw_core.config.load_profiles``）。

每個 ``contracts/golden/profiles_fixtures/<case>/`` 目錄為一列；另加一列 ``missing_dir_case``（目錄不存在，
對應 09 §2.6 第 1 點）。產生器以 ``HOME=/golden/home`` 執行，使 ``~`` 展開結果固定。

結果序列化：``dataclasses.asdict(LoadResult)`` → 把 fixture 絕對路徑替換為 ``${FIXTURE}`` →
``json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))``。
Rust 端 ``tests/common::load_result_json`` 以 ``pyjson`` ``CLI_STDOUT`` 產生同形輸出。

例外記錄：``YAMLError``（任何 ``yaml.YAMLError``）；``ValueError:<str(e)>``（含 ``UnicodeDecodeError``，
因其為 ``ValueError`` 子類）；``OSError``。未知例外型別會使產生器失敗，不寫入 golden。

產生器可 import ``sw_core.config``（golden 產生器例外，因其在擷取 Python 基線），不得 import 其他 sw_core 模組。

用法::

    python3 scripts/rust/goldens/gen_profiles.py --out contracts/golden/load_profiles.tsv
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import yaml  # noqa: E402
from _tsv import write_tsv  # noqa: E402

REPO = _DIR.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sw_core.config import load_profiles  # noqa: E402

BASELINE_PYYAML = "6.0.3"
FAKE_HOME = "/golden/home"
FIXTURES = REPO / "contracts" / "golden" / "profiles_fixtures"
MISSING_CASE = "missing_dir_case"
OP = "load_profiles"


def classify(exc: BaseException) -> str:
    """把 Python 例外對應為 golden 的錯誤記錄；未知型別直接失敗。"""
    if isinstance(exc, yaml.YAMLError):
        return "YAMLError"
    if isinstance(exc, ValueError):  # 含 UnicodeDecodeError（ValueError 子類）
        return f"ValueError:{exc}"
    if isinstance(exc, OSError):
        return "OSError"
    raise RuntimeError(f"未知例外型別，無法記錄為 golden：{type(exc).__name__}: {exc}") from exc


def run_case(profile_dir: str) -> str:
    try:
        result = load_profiles(profile_dir)
    except Exception as exc:  # noqa: BLE001  由 classify 決定是否接受
        return classify(exc)
    text = json.dumps(dataclasses.asdict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.replace(profile_dir, "${FIXTURE}")


def build_rows() -> list[tuple[str, str, str, str]]:
    os.environ["HOME"] = FAKE_HOME
    base = os.path.realpath(FIXTURES)
    cases = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())
    rows: list[tuple[str, str, str, str]] = []
    for i, name in enumerate(cases, start=1):
        rows.append((f"p{i:03d}", OP, name, run_case(os.path.join(base, name))))
    rows.append((f"p{len(cases) + 1:03d}", OP, MISSING_CASE, run_case(os.path.join(base, MISSING_CASE))))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 load_profiles golden TSV（sw_core.config 為 oracle）")
    ap.add_argument("--out", required=True, help="輸出 TSV 路徑")
    args = ap.parse_args(argv)
    if yaml.__version__ != BASELINE_PYYAML:
        print(
            f"warning: golden 基準為 PyYAML {BASELINE_PYYAML}，目前 {yaml.__version__}；輸出可能與 contracts/golden 不同",
            file=sys.stderr,
        )
    rows = build_rows()
    write_tsv(args.out, rows)
    print(f"load_profiles golden：{len(rows)} 列", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
