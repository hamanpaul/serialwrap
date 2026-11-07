"""假 flasher（P8 Task 0；計畫 09 Task 0、Task 9 黑箱）。

模擬 `ocp-mcu-upgrade` 之類的 MCU flasher：開啟 `/dev/ttyMCU`（或任一 tty 路徑），設 raw，
`baud` 有值時 `cfsetispeed／cfsetospeed`（`None`＝不改 speed，用來驗證 daemon 的 fallback baud）；
`excl=True` 時開啟後 `ioctl(TIOCEXCL)`，`close()` 時**不**清除（模擬自設獨占、不收尾的 flasher）。
POSIX 限定（termios）。stdlib only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import time
from pathlib import Path


def baud_constant(baud: int) -> int:
    """`termios.B<baud>`；不支援的速率 → ValueError。"""
    import termios

    const = getattr(termios, f"B{int(baud)}", None)
    if const is None:
        raise ValueError(f"termios 不支援 baud {baud}")
    return int(const)


class FlasherReadTimeout(TimeoutError):
    """`read_exact` 逾時；`partial` 為逾時前已收到的 bytes。"""

    def __init__(self, want: int, partial: bytes) -> None:
        super().__init__(f"read_exact 逾時：要 {want} bytes，只收到 {partial!r}")
        self.partial = partial


class FakeFlasher:
    def __init__(self, link: Path | str, *, baud: int | None, excl: bool = False) -> None:
        if os.name != "posix":
            raise RuntimeError("FakeFlasher 僅支援 POSIX 平台")
        import fcntl
        import termios
        import tty

        self.link = Path(link)
        self.excl = bool(excl)
        self.fd: int | None = os.open(os.fspath(self.link), os.O_RDWR | os.O_NOCTTY)
        try:
            attrs = termios.tcgetattr(self.fd)
            tty.cfmakeraw(attrs)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            if baud is not None:
                attrs[4] = attrs[5] = baud_constant(baud)
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            if self.excl:
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def _require_fd(self) -> int:
        if self.fd is None:
            raise ValueError("FakeFlasher 已關閉")
        return self.fd

    def write(self, b: bytes) -> None:
        fd = self._require_fd()
        view = memoryview(bytes(b))
        while view:
            n = os.write(fd, view)
            view = view[n:]

    def read_exact(self, n: int, timeout: float) -> bytes:
        """讀滿 `n` bytes；期限內不足 → `FlasherReadTimeout`（附已收到的部分）。"""
        fd = self._require_fd()
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise FlasherReadTimeout(n, buf)
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            try:
                chunk = os.read(fd, n - len(buf))
            except OSError:
                raise FlasherReadTimeout(n, buf) from None
            if not chunk:
                raise FlasherReadTimeout(n, buf)
            buf += chunk
        return buf

    def set_baud(self, baud: int) -> None:
        import termios

        fd = self._require_fd()
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = baud_constant(baud)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "FakeFlasher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["FakeFlasher", "FlasherReadTimeout", "baud_constant"]
