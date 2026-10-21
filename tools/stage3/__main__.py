"""Stage 3 命令列入口（`python3 -m tools.stage3 <子命令>`）。

目前接線的子命令：
- `retirement`（P9-T16）：整段 argv 原樣交給 `retirement.main`；`release.yml` 與 `mkbundle.sh` 的既有呼叫方式不變；
- `od09 check`（P9-T15）：唯讀委派 `od09_consume.check_main`，只呼叫 P2 `od09_freeze.check_frozen_before`。

P9-T17 擴充其餘子命令時於 `build_parser` 新增登錄，不得改動 `retirement` 的呼叫路徑。
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from tools.stage3.retirement import main as retirement_main


def build_parser() -> argparse.ArgumentParser:
    """登錄所有子命令（供說明與參數驗證）；`retirement` 的實際解析由 `retirement.main` 負責。"""
    from tools.stage3 import od09_consume

    parser = argparse.ArgumentParser(prog="python3 -m tools.stage3")
    commands = parser.add_subparsers(dest="command", required=True)
    retirement = commands.add_parser("retirement", help="Python daemon 退役切換（P9-T16）")
    retirement.add_argument("rest", nargs=argparse.REMAINDER, help="交給 retirement 的子命令與參數")
    od09 = commands.add_parser("od09", help="OD-09 凍結結果唯讀核對（P9-T15）")
    actions = od09.add_subparsers(dest="action", required=True)
    od09_consume.add_check_arguments(actions.add_parser("check", help="核對門檻檔於 run 起點前是否已凍結（唯讀）"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    if tokens[:1] == ["retirement"]:
        return retirement_main(tokens)
    parsed = build_parser().parse_args(tokens)  # 未登錄的子命令、缺少子命令或缺參數在此以 exit 2 結束
    if parsed.command != "od09":
        return 2
    from tools.stage3 import od09_consume

    return od09_consume.check_main(tokens)


if __name__ == "__main__":
    raise SystemExit(main())
