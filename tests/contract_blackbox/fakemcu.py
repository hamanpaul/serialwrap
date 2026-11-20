"""假 MCU（TI SBL 子集；P8 Task 0；計畫 09 Task 0、Task 9 黑箱）。

DUT 端 PTY pair：daemon（或測試）開 slave 當 MCU 的 UART，本模組在 master 端扮演 MCU bootloader：
- 未同步時收到 `55 55`（auto-baud sync）→ 回 `00 cc`（ACK），進入已同步；
- 已同步後每個封包（首 byte 為封包總長 N，含長度與 checksum byte，共 N bytes）收齊即回 `00 cc`；
  封包邊界上再次出現 `55 55` 視為重新同步（同一 daemon 連續兩次 flash）；
- 收到的全部 bytes 記入 `rx_log()`（外部 oracle）；`current_speed()` 回 slave 端目前的 termios ospeed（`termios.B*` 常數）。
slave 無人開啟時 master 讀取得 EIO，每 0.02 s 重試。POSIX 限定。stdlib only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import threading
from pathlib import Path

from shelltarget import open_dut_pty

SYNC = b"\x55\x55"
ACK = b"\x00\xcc"


class FakeMcu:
    def __init__(self, *, by_id_name: str = "mcu") -> None:
        self.by_id_name = by_id_name
        self.slave_path: str = ""
        self._master: int | None = None
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        self._rx = bytearray()
        self._pending = bytearray()
        self._synced = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeMcu":
        self._master, self.slave_path = open_dut_pty()
        self._wake_r, self._wake_w = os.pipe()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fake-mcu", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        master, wake = self._master, self._wake_r
        assert master is not None and wake is not None
        while not self._stop.is_set():
            ready, _, _ = select.select([master, wake], [], [], 0.2)
            if wake in ready or self._stop.is_set():
                break
            if master not in ready:
                continue
            try:
                data = os.read(master, 4096)
            except BlockingIOError:
                continue
            except OSError:
                # slave 無人開啟（EIO）：等下一次開啟
                if self._stop.wait(0.02):
                    break
                continue
            if not data:
                continue
            with self._lock:
                self._rx += data
                self._pending += data
                replies = self._consume()
            for _ in range(replies):
                self._write(ACK)

    def _consume(self) -> int:
        """解析 `_pending`，回傳應回幾個 ACK（持鎖呼叫）。"""
        acks = 0
        buf = self._pending
        while True:
            if not self._synced:
                i = buf.find(SYNC)
                if i < 0:
                    # 保留最後一個 byte（可能是 sync 的前半）
                    del buf[: max(0, len(buf) - 1)]
                    return acks
                del buf[: i + len(SYNC)]
                self._synced = True
                acks += 1
                continue
            if len(buf) >= 2 and buf[:2] == SYNC:
                del buf[:2]
                acks += 1
                continue
            if not buf:
                return acks
            size = buf[0]
            if size < 2:
                # 長度不合法：丟棄這個 byte，不回應
                del buf[:1]
                continue
            if len(buf) < size:
                return acks
            del buf[:size]
            acks += 1

    def _write(self, data: bytes) -> None:
        master = self._master
        if master is None:
            return
        try:
            os.write(master, data)
        except OSError:
            pass

    def rx_log(self) -> bytes:
        with self._lock:
            return bytes(self._rx)

    def current_speed(self) -> int:
        """slave 端目前的 ospeed（`termios.B*` 常數；Linux 對 master fd 的 termios ioctl 作用於 slave）。"""
        import termios

        if self._master is None:
            raise RuntimeError("FakeMcu 尚未啟動")
        return int(termios.tcgetattr(self._master)[5])

    def reset(self) -> None:
        """回到未同步狀態（模擬 MCU 重新上電）；`rx_log` 保留。"""
        with self._lock:
            self._synced = False
            self._pending.clear()

    def link(self, by_id_dir: Path) -> Path:
        """在 `by_id_dir` 建立 `by_id_name` → slave 的 symlink（已存在則取代）。"""
        link = Path(by_id_dir) / self.by_id_name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(self.slave_path, link)
        return link

    def stop(self) -> None:
        self._stop.set()
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        for name in ("_master", "_wake_r", "_wake_w"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)

    def __enter__(self) -> "FakeMcu":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["FakeMcu", "SYNC", "ACK"]
