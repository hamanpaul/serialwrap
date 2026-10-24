#!/usr/bin/env python3
"""產生 ``contracts/golden/human_console_mode.tsv``（P6 Task 3；``sw_core.service._human_console_mode`` 為基準）。

四欄 TSV（P3 ``_tsv.write_tsv``）：``<case_id>\\tline_mode\\t<hex(命令 UTF-8)>\\t<hex("line"|"interactive")>``。
內建 seed ``20261015``：固定案例（對照 ``tests/test_service_human_console.py`` 與 07 Task 3 表）＋隨機組合
（引號、跳脫、env 賦值、sudo 帶值選項與尾端缺值、``env``／``command``／``builtin``／``exec`` 前綴、引號未閉合），共 ≥300 列。

用法::

    python3 scripts/rust/goldens/gen_human_line_mode.py --out contracts/golden/human_console_mode.tsv
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
for p in (ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _tsv import Row, write_tsv  # noqa: E402
from sw_core.service import _human_console_mode  # noqa: E402

SEED = 20261015
RANDOM_CASES = 400

FIXED = [
    "vim /etc/config", "ls -la", "sudo vim x", "sudo -u root top", "FOO=1 BAR=2 less x", "env -i A=1 htop",
    "exec watch date", "-- top", "/usr/bin/vi f", "vim 'unterminated", "   ", "", "sudo", "sudo -u", "sudo -u root",
    "sudo -- vim", "sudo -E -- htop", "env", "env --", "env -- top", "env A=1", "command vim", "builtin less x",
    "exec", "A=1", "A=1 B", "1A=2 vim", "_x=y nano", "\"vim\" file", "'top'", "v\\im", "vim\\", "\"less",
    "sudo -u root -g wheel tmux", "sudo -p prompt screen", "sudo -h host btop", "nice top", "tig status",
    "watch -n1 date", "/bin/../usr/bin/less", "./vim", "vim/", "VIM", "  top  ", "\ttop", "top\n", "a\nvim",
    "sudo -C 3 -T 10 -r role -t type vimdiff", "env -u X -i htop", "command -v vim", "-- -- nmtui",
    "menuconfig", "alsamixer", "more file", "most file", "view x", "screen -r", "tmux attach", "btop",
    "echo vim", "cat | less", "FOO='a b' less", "FOO=\"x y\" top", "sudo FOO=1 vim", "env sudo vim",
    "sudo env vim", "exec sudo -u a htop", "command command vim", "中文 vim", "vim 中文", "\"\" vim",
    "'' top", "sudo '' vim", "env '' top",
]

TOKENS = ["vim", "ls", "top", "less", "htop", "sudo", "env", "exec", "command", "builtin", "--", "-u", "root",
          "-i", "-E", "A=1", "B_2=x", "1X=y", "/usr/bin/vim", "./top", "'vim'", "\"less x\"", "'unterminated",
          "\"open", "back\\slash", "x\\", "-g", "wheel", "watch", "nano", "echo", "-p", "", "中", "tig"]


def build_rows() -> list[Row]:
    rng = random.Random(SEED)
    cmds = list(FIXED)
    for _ in range(RANDOM_CASES):
        n = rng.randint(0, 6)
        sep = rng.choice([" ", "  ", "\t", " "])
        cmds.append(sep.join(rng.choice(TOKENS) for _ in range(n)))
    rows: list[Row] = []
    for i, cmd in enumerate(cmds):
        rows.append((f"hlm-{i:04d}", "line_mode", cmd, _human_console_mode(cmd)))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 human_console_mode.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_tsv(a.out, build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
