"""PTY 假 DUT。

使用 pty.openpty() 與獨立 thread 狀態機模擬 DUT（開機 prompt、login、password、prompt、echo）。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。

P5-T15a（測試資產變更，規格 §10.4）向後相容擴充；既有參數與預設行為不變：
- 建構子 keyword-only 參數 `login`（False 時開機直接送 shell prompt）、`boot_banner`
  （於第一個 prompt 之前送出）、`noise_interval_s`（週期性送出雜訊行）。
- 方法 `emit(data)`（立即從 DUT 端送出）、`written()`（= `rx_log()` 別名）、
  `pause_responding(seconds)`（期間收到的輸入照常記錄，但所有回應延到暫停結束後才送出）。
- 行內命令：`sleep N`（N 秒後送 prompt）、`yes | head -c N`（恰送 N bytes 的 `y\\n` 後送 prompt）、
  `printf M<a>..M<b>`（分 10 個 chunk 送出 `M<a>`～`M<b>` 各一行後送 prompt，供 F4 背景分頁）。
P6 Task 11（07 計畫；測試資產變更，規格 §10.4 需獨立審閱）再追加 keyword-only 選用能力，預設行為不變：
- `scripts`：`{命令行 bytes: (延遲秒, 回覆 bytes[, 是否補 prompt])}`；收到該行時先回顯，延遲後送回覆，
  第三欄省略或為 True 時接著送 prompt（False 時不回 prompt，模擬 `bootcmd` 之類卡在 boot 的命令）。
- `ready=False`：不回應含 `__READY__` 的行（不回顯、不送 prompt），ready nonce 永遠等不到，session 停在 ATTACHED。
- `hold_reply(on)`：True 時暫停所有回應（輸入照常記錄），False 時解除並送出累積的回應。
回應一律依排程佇列送出（FIFO、命令間序列化，如同真實 shell 依序執行）；`^C` 會取消尚未送出的
命令輸出並立即回 `^C` 與 prompt。
"""
from __future__ import annotations

import heapq
import os
import re
import select
import threading
import time
from pathlib import Path

_SLEEP_RE = re.compile(r"^sleep\s+(\d+(?:\.\d+)?)$")
_YES_HEAD_RE = re.compile(r"^yes\s*\|\s*head\s+-c\s+(\d+)$")
_PRINTF_RANGE_RE = re.compile(r"^printf\s+M(\d+)\.\.M(\d+)$")

# printf 範圍輸出的分段數與段間隔（秒）
_PRINTF_CHUNKS = 10
_PRINTF_CHUNK_GAP_S = 0.05
# 單次寫入 master 的上限，避免大量輸出長時間獨佔寫鎖
_WRITE_SLICE = 4096


class FakeTarget:
    def __init__(
        self,
        workdir: Path,
        *,
        prompt: str = "root@fake:~# ",
        user: str = "root",
        password: str = "pw",
        login: bool = True,
        boot_banner: bytes = b"",
        noise_interval_s: float | None = None,
        scripts: dict[bytes, tuple] | None = None,
        ready: bool = True,
    ) -> None:
        self.workdir = Path(workdir)
        self.prompt = prompt
        self.user = user
        self.password = password
        self.login = bool(login)
        self.boot_banner = bytes(boot_banner)
        if noise_interval_s is not None and noise_interval_s <= 0:
            raise ValueError("noise_interval_s 必須為正數或 None")
        self.noise_interval_s = noise_interval_s
        self.scripts: dict[bytes, tuple] = dict(scripts or {})
        for key, spec in self.scripts.items():
            if not isinstance(key, bytes) or not isinstance(spec, tuple) or len(spec) not in (2, 3):
                raise ValueError("scripts 格式為 {bytes: (延遲秒, 回覆 bytes[, 是否補 prompt])}")
        self.ready = bool(ready)
        self.master_fd: int | None = None
        self.slave_fd: int | None = None
        self.slave_path: str | None = None
        self.link_path: Path | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rx_log = bytearray()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._thread_exc: BaseException | None = None
        # 排程佇列：(due_monotonic, seq, data, kind)；kind 為 "resp"（狀態機回應）或 "noise"
        self._sched_lock = threading.Lock()
        self._sched: list[tuple[float, int, bytes, str]] = []
        self._seq = 0
        self._tail_due = 0.0  # 最後一筆回應的排程時刻（維持 FIFO 與命令序列化）
        self._pause_until = 0.0
        self._hold = False  # hold_reply(True)；與 pause_responding 的期限各自獨立、可疊加
        self._noise_count = 0
        self._wake_r: int | None = None
        self._wake_w: int | None = None

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------

    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path:
        if os.name != "posix":
            raise RuntimeError("FakeTarget 僅支援 POSIX 平台")
        import pty
        import termios

        self.master_fd, self.slave_fd = pty.openpty()
        attrs = termios.tcgetattr(self.slave_fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.slave_fd, termios.TCSANOW, attrs)
        self.slave_path = os.ttyname(self.slave_fd)
        # master 改非阻塞：寫入以 select 等可寫，stop 時不會卡死在 os.write
        os.set_blocking(self.master_fd, False)
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)

        by_id_dir = Path(by_id_dir)
        by_id_dir.mkdir(parents=True, exist_ok=True)
        self.link_path = by_id_dir / name
        if self.link_path.is_symlink() or self.link_path.exists():
            self.link_path.unlink()
        os.symlink(self.slave_path, self.link_path)

        with self._sched_lock:
            self._sched.clear()
            self._tail_due = 0.0
            self._pause_until = 0.0
            self._hold = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return self.link_path

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        for fd in (self.master_fd, self.slave_fd, self._wake_r, self._wake_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = None
        self.slave_fd = None
        self._wake_r = None
        self._wake_w = None
        if self.link_path and self.link_path.is_symlink():
            try:
                self.link_path.unlink()
            except OSError:
                pass
        self.link_path = None

    # ------------------------------------------------------------------
    # 對外操作（測試端呼叫）
    # ------------------------------------------------------------------

    def emit(self, data: bytes) -> None:
        """立即從 DUT 端送出 `data`（不經排程、不受 pause_responding 影響、不附加任何內容）。"""
        if self.master_fd is None:
            raise RuntimeError("FakeTarget 尚未 start()")
        self._write_all(bytes(data))

    def written(self, timeout_s: float = 1.0) -> bytes:
        """DUT 已收到的全部 bytes（`rx_log()` 的別名）。"""
        return self.rx_log(timeout_s)

    def pause_responding(self, seconds: float) -> None:
        """暫停回應 `seconds` 秒：期間輸入照常讀取與記錄，所有回應（含雜訊）延到暫停結束後送出。"""
        with self._sched_lock:
            self._pause_until = max(self._pause_until, time.monotonic() + float(seconds))
        self._wake()

    def hold_reply(self, on: bool) -> None:
        """True：暫停所有回應直到以 False 解除（輸入照常讀取與記錄）；False：解除並送出累積的回應。

        與 `pause_responding(seconds)` 可疊加：兩者以獨立旗標／期限判定（任一生效即暫停），
        `hold_reply(False)` 不會取消尚未到期的 `pause_responding`（審查 3317987_d6e3b3c-opus Low）。"""
        with self._sched_lock:
            self._hold = bool(on)
        self._wake()

    def rx_log(self, timeout_s: float = 1.0) -> bytes:
        if self._thread_exc is not None:
            raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                break
            if self._thread is not None and not self._thread.is_alive():
                if self._thread_exc is not None:
                    raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
                break
            if self.master_fd is None:
                break
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0)
                if not rlist:
                    break
            except (OSError, ValueError):
                break
            time.sleep(0.01)

        if self._thread_exc is not None:
            raise RuntimeError(f"FakeTarget reader thread died: {self._thread_exc}")
        with self._lock:
            return bytes(self._rx_log)

    # ------------------------------------------------------------------
    # 內部：寫入與排程
    # ------------------------------------------------------------------

    def _wake(self) -> None:
        if self._wake_w is not None:
            try:
                os.write(self._wake_w, b"\0")
            except OSError:
                pass

    def _write_all(self, data: bytes) -> None:
        """把 data 完整寫入 master；以 select 等可寫並分段，stop 時中止。"""
        with self._write_lock:
            view = memoryview(data)
            while view:
                fd = self.master_fd
                if fd is None or self._stop.is_set():
                    raise OSError("FakeTarget 已停止")
                try:
                    _, wlist, _ = select.select([], [fd], [], 0.05)
                except (OSError, ValueError) as e:
                    raise OSError(str(e)) from e
                if not wlist:
                    continue
                try:
                    n = os.write(fd, view[:_WRITE_SLICE])
                except BlockingIOError:
                    continue
                view = view[n:]

    def _schedule(self, data: bytes, delay_s: float = 0.0) -> None:
        """排一筆狀態機回應：接在前一筆回應之後（FIFO），再延後 delay_s 秒。"""
        with self._sched_lock:
            due = max(time.monotonic(), self._tail_due) + delay_s
            self._tail_due = due
            self._seq += 1
            heapq.heappush(self._sched, (due, self._seq, data, "resp"))

    def _cancel_pending_responses(self) -> None:
        with self._sched_lock:
            self._sched = [it for it in self._sched if it[3] != "resp"]
            heapq.heapify(self._sched)
            self._tail_due = 0.0

    def _pop_due(self, now: float) -> tuple[list[bytes], float | None]:
        """取出已到期的項目；回傳 (待送資料, 距下一筆到期的秒數或 None)。"""
        out: list[bytes] = []
        with self._sched_lock:
            paused = self._hold or now < self._pause_until
            if not paused:
                while self._sched and self._sched[0][0] <= now:
                    out.append(heapq.heappop(self._sched)[2])
            if self._hold:
                nxt = None  # 等 hold_reply(False) 的 wake
            elif paused:
                nxt = self._pause_until - now
            elif self._sched:
                nxt = max(0.0, self._sched[0][0] - now)
            else:
                nxt = None
        return out, nxt

    def _schedule_noise(self, at: float) -> None:
        with self._sched_lock:
            self._noise_count += 1
            self._seq += 1
            line = f"[{self._noise_count:8d}] fake-dut: noise tick\r\n".encode("ascii")
            heapq.heappush(self._sched, (at, self._seq, line, "noise"))

    # ------------------------------------------------------------------
    # 內部：狀態機
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        assert self.master_fd is not None
        prompt_b = self.prompt.encode("utf-8")
        boot = self.boot_banner + (b"login: " if self.login else prompt_b)
        try:
            self._write_all(boot)
        except OSError:
            return

        state = "LOGIN" if self.login else "READY"
        line_buf = bytearray()
        prev_was_cr = False
        next_noise = (
            time.monotonic() + self.noise_interval_s if self.noise_interval_s is not None else None
        )

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if next_noise is not None and now >= next_noise:
                    self._schedule_noise(now)
                    next_noise = now + self.noise_interval_s  # type: ignore[operator]
                pending, nxt = self._pop_due(now)
                for data in pending:
                    self._write_all(data)
                if pending:
                    continue

                timeout = 0.05
                if nxt is not None:
                    timeout = min(timeout, nxt)
                if next_noise is not None:
                    timeout = min(timeout, max(0.0, next_noise - now))
                fds = [self.master_fd]
                if self._wake_r is not None:
                    fds.append(self._wake_r)
                try:
                    rlist, _, _ = select.select(fds, [], [], timeout)
                except (OSError, ValueError):
                    break
                if self._wake_r is not None and self._wake_r in rlist:
                    try:
                        os.read(self._wake_r, 4096)
                    except OSError:
                        pass
                if self.master_fd not in rlist:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break

                with self._lock:
                    self._rx_log.extend(chunk)

                for b in chunk:
                    byte = bytes([b])
                    if byte == b"\n" and prev_was_cr:
                        prev_was_cr = False
                        continue
                    prev_was_cr = (byte == b"\r")

                    if state == "LOGIN":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.user:
                                self._schedule(b"\r\nPassword: ")
                                state = "PASSWORD"
                            else:
                                self._schedule(b"\r\nlogin: ")
                        else:
                            line_buf.append(b)
                    elif state == "PASSWORD":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.password:
                                self._schedule(b"\r\n" + prompt_b)
                                state = "READY"
                            else:
                                self._schedule(b"\r\nLogin incorrect\r\nlogin: ")
                                state = "LOGIN"
                        else:
                            line_buf.append(b)
                    elif state == "READY":
                        if byte == b"\x03":
                            line_buf.clear()
                            self._cancel_pending_responses()
                            self._schedule(b"^C\r\n" + prompt_b)
                        elif byte in (b"\r", b"\n"):
                            cmd = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            self._run_command(cmd, prompt_b)
                        else:
                            line_buf.append(b)
        except OSError:
            pass
        except BaseException as e:
            self._thread_exc = e

    def _run_command(self, cmd: str, prompt_b: bytes) -> None:
        """把一行命令的回顯、輸出與 prompt 排入回應佇列。"""
        echo = (cmd + "\r\n").encode("utf-8")

        if not self.ready and "__READY__" in cmd:
            return
        spec = self.scripts.get(cmd.encode("utf-8"))
        if spec is not None:
            delay_s, reply = float(spec[0]), bytes(spec[1])
            self._schedule(echo)
            self._schedule(reply, delay_s=delay_s)
            if len(spec) < 3 or spec[2]:
                self._schedule(prompt_b)
            return

        m = _SLEEP_RE.match(cmd)
        if m:
            self._schedule(echo)
            self._schedule(prompt_b, delay_s=float(m.group(1)))
            return

        m = _YES_HEAD_RE.match(cmd)
        if m:
            n = int(m.group(1))
            payload = (b"y\n" * (n // 2 + 1))[:n]
            self._schedule(echo + payload + prompt_b)
            return

        m = _PRINTF_RANGE_RE.match(cmd)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            lines = [f"M{i}\r\n".encode("ascii") for i in range(lo, hi + 1)] if lo <= hi else []
            self._schedule(echo)
            if lines:
                per = -(-len(lines) // _PRINTF_CHUNKS)  # 向上取整
                for i in range(0, len(lines), per):
                    self._schedule(b"".join(lines[i : i + per]), delay_s=_PRINTF_CHUNK_GAP_S)
            self._schedule(prompt_b, delay_s=_PRINTF_CHUNK_GAP_S)
            return

        out = echo
        if cmd.startswith("echo "):
            out += (cmd[5:] + "\r\n").encode("utf-8")
        elif cmd == "echo":
            out += b"\r\n"
        self._schedule(out + prompt_b)
