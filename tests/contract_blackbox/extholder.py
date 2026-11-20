"""外部 tty 持有者（P8 Task 0；計畫 09 Task 0、Task 11）。

`ExternalHolder(path, flock=...)` 以**單一子行程**（直接執行 Python 直譯器，不經 shell wrapper）開啟並持有 `path`：
- `flock=False`：只 `open(O_RDWR|O_NOCTTY)`，模擬 minicom 之類不取 advisory lock、不設 TIOCEXCL 的工具；
- `flock=True`：open 後 `fcntl.flock(LOCK_EX|LOCK_NB)`，模擬遵守 advisory lock 的工具（鎖被占用時立即失敗，不無限等待）。

`start()` 等子行程回報開啟結果：open 或 flock 失敗（含 daemon 關閉前未清 TIOCEXCL 造成的 `EBUSY`，R0-F3）一律
`raise ExternalHolderOpenError(errno)`，**不得**靜默視為持有中。`stop()` 關閉子行程 stdin（子行程讀到 EOF 即關檔結束），
逾時才 SIGKILL。POSIX 限定。stdlib only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import subprocess
import sys
import time

# 子行程：開啟（＋flock）後回報一行 "OK" 或 "ERR <errno>"，之後阻塞讀 stdin 直到 EOF。
_CHILD = r"""
import os, sys
path, use_flock = sys.argv[1], sys.argv[2] == "1"
try:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    if use_flock:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as e:
    sys.stdout.write("ERR %d\n" % (e.errno or 0))
    sys.stdout.flush()
    sys.exit(1)
sys.stdout.write("OK\n")
sys.stdout.flush()
sys.stdin.buffer.read()
os.close(fd)
"""


class ExternalHolderOpenError(OSError):
    """外部持有者開啟（或 flock）失敗；`errno` 為子行程看到的 errno。"""


class ExternalHolder:
    def __init__(self, path: str, *, flock: bool) -> None:
        self.path = str(path)
        self.flock = bool(flock)
        self.pid: int | None = None
        self._proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 10.0) -> None:
        if os.name != "posix":
            raise RuntimeError("ExternalHolder 僅支援 POSIX 平台")
        if self._proc is not None:
            raise RuntimeError("ExternalHolder 已啟動")
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _CHILD, self.path, "1" if self.flock else "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        line = self._read_line(proc, timeout_s)
        if line == "OK":
            self._proc = proc
            self.pid = proc.pid
            return
        self._reap(proc)
        if line.startswith("ERR "):
            err = int(line.split()[1])
            raise ExternalHolderOpenError(err, os.strerror(err), self.path)
        raise ExternalHolderOpenError(0, f"外部持有者未回報開啟結果（{line!r}）", self.path)

    @staticmethod
    def _read_line(proc: subprocess.Popen, timeout_s: float) -> str:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        buf = b""
        deadline = time.monotonic() + timeout_s
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\n", 1)[0].decode("ascii", errors="replace").strip()

    @staticmethod
    def _reap(proc: subprocess.Popen, timeout_s: float = 2.0) -> None:
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout_s)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._reap(proc)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> "ExternalHolder":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["ExternalHolder", "ExternalHolderOpenError"]
