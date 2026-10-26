#!/usr/bin/env python3
"""產生 ``contracts/golden/human_console_mode.tsv`` 與 ``human_console_mode.windows.tsv``（P6 Task 3；``sw_core.service._human_console_mode`` 為基準）。

四欄 TSV（P3 ``_tsv.write_tsv``）：``<case_id>\\tline_mode\\t<hex(命令 UTF-8)>\\t<hex("line"|"interactive")>``。
內建 seed ``20261015``：固定案例（對照 ``tests/test_service_human_console.py`` 與 07 Task 3 表）＋隨機組合
（引號、跳脫、env 賦值、sudo 帶值選項與尾端缺值、``env``／``command``／``builtin``／``exec`` 前綴、引號未閉合），共 ≥300 列。

``--out`` 檔名以 ``.windows.tsv`` 結尾時改產生 Windows 版（sol 補審 P6 #2）：``_human_console_mode`` 內的
``os.path.basename`` 在 Windows Python 為 ``ntpath.basename``（反斜線與 drive prefix 皆為分隔），故以 ``ntpath``
取代 ``os.path`` 呼叫同一函式；語料為 POSIX 全部案例＋Windows 路徑固定案例＋另一 seed 的隨機組合。
POSIX 版輸出與既有 golden 逐位元組相同。

用法::

    python3 scripts/rust/goldens/gen_human_line_mode.py --out contracts/golden/human_console_mode.tsv
    python3 scripts/rust/goldens/gen_human_line_mode.py --out contracts/golden/human_console_mode.windows.tsv
"""

from __future__ import annotations

import argparse
import ntpath
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
for p in (ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _tsv import Row, write_tsv  # noqa: E402
import sw_core.service as _service  # noqa: E402
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

WIN_SEED = 20261026
WIN_RANDOM_CASES = 200

# Windows 路徑（shlex posix 模式下單引號內反斜線保留；``ntpath.basename`` 以 ``\``／``/`` 與 drive prefix 分隔）
WIN_FIXED = [
    "'C:\\bin\\vim' file", "'C:vim' file", "C:top", "'C:\\Windows\\sudo' vim", "'D:env' htop",
    "'C:\\x\\command' less", "'\\\\srv\\share\\top'", "'a\\b\\ls' x", "'a\\b\\nano' x", "'C:/bin/less'",
    "'C:\\bin\\vim\\'", "'C:'", "'C:\\'", "'c:\\Tools\\htop.exe'", "sudo 'C:\\bin\\top'",
    "env 'A=1' 'C:\\bin\\watch' date", "'\\\\?\\C:\\bin\\tmux'", "'Z:screen' -r", "C:\\vim",
]
WIN_TOKENS = ["'C:\\bin\\vim'", "'C:vim'", "C:top", "'D:env'", "'C:\\Windows\\sudo'", "'\\\\h\\s\\less'",
              "'a\\b\\htop'", "'C:'", "'x\\'"]


def _random_cmds(rng: random.Random, tokens: list[str], count: int) -> list[str]:
    cmds: list[str] = []
    for _ in range(count):
        n = rng.randint(0, 6)
        sep = rng.choice([" ", "  ", "\t", " "])
        cmds.append(sep.join(rng.choice(tokens) for _ in range(n)))
    return cmds


def build_rows() -> list[Row]:
    cmds = list(FIXED) + _random_cmds(random.Random(SEED), TOKENS, RANDOM_CASES)
    rows: list[Row] = []
    for i, cmd in enumerate(cmds):
        rows.append((f"hlm-{i:04d}", "line_mode", cmd, _human_console_mode(cmd)))
    return rows


def build_windows_rows() -> list[Row]:
    """Windows Python 行為：``_human_console_mode`` 內 ``os.path`` 即 ``ntpath``。"""
    posix_cmds = list(FIXED) + _random_cmds(random.Random(SEED), TOKENS, RANDOM_CASES)
    cmds = posix_cmds + WIN_FIXED + _random_cmds(random.Random(WIN_SEED), TOKENS + WIN_TOKENS, WIN_RANDOM_CASES)
    saved = _service.os.path
    _service.os.path = ntpath
    try:
        return [(f"hlmw-{i:04d}", "line_mode", cmd, _human_console_mode(cmd)) for i, cmd in enumerate(cmds)]
    finally:
        _service.os.path = saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="產生 human_console_mode.tsv golden")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    write_tsv(a.out, build_windows_rows() if a.out.name.endswith(".windows.tsv") else build_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
