"""以 daemon_prefix() 啟動的 Python daemon 包裝（P3-T2 紅燈階段 stub）。

實作於 Step 4：Popen、socket＋health.ping 輪詢、daemon.stop 與 SIGTERM→SIGKILL 收尾、
並把 pid 記錄追加到 $SW_AB_PIDLOG。紅燈階段建構即 raise。
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


class PyDaemon:
    def __init__(self, env: Mapping[str, str], *, profile_dir: Path, socket: str, lock: Path) -> None:
        raise NotImplementedError("P3-T2")

    def start(self, timeout_s: float = 15.0) -> None:
        raise NotImplementedError("P3-T2")

    def stop(self, timeout_s: float = 5.0) -> None:
        raise NotImplementedError("P3-T2")

    @property
    def pids(self) -> list[int]:
        raise NotImplementedError("P3-T2")
