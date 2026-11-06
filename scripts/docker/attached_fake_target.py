"""attached-legacy 情境用的 PTY 假 target（以 swtest 身分在容器內常駐）。

開一對 pty、把 slave 路徑寫進 argv[1] 指定的檔案後持續排空 master，讓 0.3.2 daemon 能 attach 該 slave
（passthrough profile 不需登入，session 停在 ATTACHED）。只用標準函式庫。收到 SIGTERM 即結束。
"""
from __future__ import annotations

import os
import pty
import select
import signal
import sys
import termios


def main() -> int:
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
    attrs[3] = 0
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    # 寫入暫存檔再 rename，driver 讀到即為完整路徑。
    tmp = sys.argv[1] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(os.ttyname(slave))
    os.rename(tmp, sys.argv[1])
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        ready, _, _ = select.select([master], [], [], 1.0)
        if ready:
            try:
                os.read(master, 4096)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
