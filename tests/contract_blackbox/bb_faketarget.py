"""PTY 假 DUT（P3-T2 紅燈階段 stub）。

實作於 Step 4：pty.openpty() 加獨立 thread 狀態機（login → Password → prompt → echo）。
紅燈階段 __init__ 僅保存參數；start／rx_log 尚未實作；stop 為 no-op（stub 階段無 PTY 可關）。
"""
from __future__ import annotations

from pathlib import Path


class FakeTarget:
    def __init__(self, workdir: Path, *, prompt: str = "root@fake:~# ", user: str = "root",
                 password: str = "pw") -> None:
        self.workdir = workdir
        self.prompt = prompt
        self.user = user
        self.password = password

    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path:
        raise NotImplementedError("P3-T2")

    def rx_log(self) -> bytes:
        raise NotImplementedError("P3-T2")

    def stop(self) -> None:
        return None  # 紅燈階段從未建立 PTY，無可釋放者
