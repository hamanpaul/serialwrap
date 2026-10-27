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
回應一律依排程佇列送出（FIFO、命令間序列化，如同真實 shell 依序執行）；`^C` 會取消尚未送出的
命令輸出並立即回 `^C` 與 prompt。

P7 Task 18A 擴充（全部 keyword-only，未給時逐位元組維持 P3 行為）：

- ``login_steps``：``[{"expect": str, "respond": str}]``；啟動時不主動輸出，每收到一行與目前步驟的
  ``expect`` 完全相等即輸出 ``respond`` 並前進，不符則重印上一個 ``respond``（尚無則不輸出）；步驟走完
  進入 READY（``[]``＝啟動即 READY 且靜默）。
- ``commands``：``{"overrides": {cmd: {"response": str}}}``；READY 下該行命令改為**原樣**輸出
  ``response``（不回顯命令、不補 prompt）。
- ``delay_ms``：對 TX 的每個回應先延遲該毫秒數。
- ``noise``：``{"interval_ms": int, "pattern": str}``；背景每隔 interval 輸出 ``pattern + "\r\n"``。
- ``console_lock``：``{"secret": str, "idle_s": float}``；未收到等於 ``secret`` 的一行前吞下所有 TX、
  不輸出任何 byte；解鎖後輸出 ``"\r\n" + prompt`` 進入 READY；``idle_s > 0`` 時，解鎖後連續
  ``idle_s`` 秒無 TX 即重新上鎖（靜默）。
- ``boot_on_start``：取代啟動時的 ``login: ``，改依 ``boot()`` 的腳本輸出。
- ``release_slave``：daemon 相容模式（同 ``test_bb_daemon_e2e.DaemonAttachableTarget``）：建立 by-id 連結後
  立即關閉自身 slave fd（否則 daemon 的外部 holder 偵測判 DeviceHeldError），reader 遇 slave 無人開啟的
  EIO 時不結束、等待下一次開啟（release／attach 循環）。``bb_p7.P7Rig.start`` 一律開啟。
- ``emit()``／``freeze_rx()``／``thaw_rx()``／``boot()``／``rx_mark()``／``rx_log_since()``：見各方法。
  freeze 期間所有輸出（含回應、雜訊、boot、emit）直接丟棄，TX 照收並記入 rx_log。

兩組擴充合併（merge P5 T15～T19 至 P7）：輸出一律經 `_write_all`（freeze 期間丟棄）；P7 的回應延遲
以排程佇列的 `delay_s` 實現（FIFO 序列化與原本 reader thread 內 sleep 等價）。
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
        login_steps: list[dict] | None = None,
        commands: dict | None = None,
        delay_ms: int = 0,
        noise: dict | None = None,
        console_lock: dict | None = None,
        boot_on_start: list[tuple[float, str]] | None = None,
        release_slave: bool = False,
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
        self.login_steps = None if login_steps is None else [dict(st) for st in login_steps]
        self.overrides: dict = dict((commands or {}).get("overrides") or {})
        self.delay_s = max(0, int(delay_ms)) / 1000.0
        self.noise = dict(noise) if noise else None
        self.console_lock = dict(console_lock) if console_lock else None
        self.boot_on_start = list(boot_on_start) if boot_on_start is not None else None
        self.release_slave = release_slave
        self._frozen = False
        self._state = "LOGIN"
        self._step = 0
        self._last_prompt: bytes | None = None
        self._last_tx = 0.0
        self._boot_gen = 0
        self._aux_threads: list[threading.Thread] = []
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

        if self.release_slave:
            os.close(self.slave_fd)
            self.slave_fd = None

        with self._sched_lock:
            self._sched.clear()
            self._tail_due = 0.0
            self._pause_until = 0.0
        self._stop.clear()
        self._reset_login_state()
        self._thread = threading.Thread(target=self._reader_main, daemon=True)
        self._thread.start()
        if self.noise:
            self._spawn(self._noise_loop)
        if self.boot_on_start is not None:
            self.boot(self.boot_on_start)
        return self.link_path

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        for t in self._aux_threads:
            t.join(timeout=2.0)
        self._aux_threads.clear()
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

    def emit(self, data: str | bytes) -> None:
        """立即從 DUT 端送出 `data`（str 以 UTF-8 編碼；不經排程、不受 pause_responding 影響、不附加任何內容；
        freeze 期間丟棄）。"""
        if self.master_fd is None:
            raise RuntimeError("FakeTarget 尚未 start()")
        self._write_all(data.encode("utf-8") if isinstance(data, str) else bytes(data))

    def written(self, timeout_s: float = 1.0) -> bytes:
        """DUT 已收到的全部 bytes（`rx_log()` 的別名）。"""
        return self.rx_log(timeout_s)

    def pause_responding(self, seconds: float) -> None:
        """暫停回應 `seconds` 秒：期間輸入照常讀取與記錄，所有回應（含雜訊）延到暫停結束後送出。"""
        with self._sched_lock:
            self._pause_until = max(self._pause_until, time.monotonic() + float(seconds))
        self._wake()

    def freeze_rx(self) -> None:
        """之後不再輸出任何 byte（模擬 read-endpoint stall），TX 仍照收。"""
        with self._write_lock:
            self._frozen = True

    def thaw_rx(self) -> None:
        with self._write_lock:
            self._frozen = False

    def boot(self, script: list[tuple[float, str]]) -> None:
        """模擬重開機：登入狀態回到初始，並依相對於呼叫時刻的秒數輸出腳本各段；新的 boot 取代舊的。"""
        with self._lock:
            self._boot_gen += 1
            gen = self._boot_gen
            self._reset_login_state()
        self._spawn(self._boot_loop, gen, time.monotonic(), list(script))

    def rx_mark(self) -> int:
        """目前 rx_log 長度，供 rx_log_since() 取之後收到的 TX。"""
        with self._lock:
            return len(self._rx_log)

    def rx_log_since(self, mark: int) -> bytes:
        return self.rx_log()[mark:]

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
        """所有輸出的唯一出口：freeze 期間丟棄；把 data 完整寫入 master（以 select 等可寫並分段，stop 時中止）。"""
        with self._write_lock:
            if self._frozen:
                return
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

    def _respond(self, data: bytes) -> None:
        """P7 `delay_ms`：每個 TX 回應先延遲 delay_s（排在前一筆回應之後）。"""
        self._schedule(data, delay_s=self.delay_s)

    def _cancel_pending_responses(self) -> None:
        with self._sched_lock:
            self._sched = [it for it in self._sched if it[3] != "resp"]
            heapq.heapify(self._sched)
            self._tail_due = 0.0

    def _pop_due(self, now: float) -> tuple[list[bytes], float | None]:
        """取出已到期的項目；回傳 (待送資料, 距下一筆到期的秒數或 None)。"""
        out: list[bytes] = []
        with self._sched_lock:
            paused = now < self._pause_until
            if not paused:
                while self._sched and self._sched[0][0] <= now:
                    out.append(heapq.heappop(self._sched)[2])
            if paused:
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
    # 內部：P7 擴充（登入狀態、背景執行緒）
    # ------------------------------------------------------------------

    def _initial_state(self) -> str:
        if self.console_lock is not None:
            return "LOCKED"
        if self.login_steps is not None:
            return "STEPS" if self.login_steps else "READY"
        return "LOGIN" if self.login else "READY"

    def _reset_login_state(self) -> None:
        self._state = self._initial_state()
        self._step = 0
        self._last_prompt = None

    def _spawn(self, fn, *args) -> None:
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._aux_threads.append(t)
        t.start()

    def _emit_quiet(self, data: bytes) -> None:
        """背景執行緒的輸出：停止中寫入失敗即忽略。"""
        try:
            self._write_all(data)
        except OSError:
            pass

    def _boot_loop(self, gen: int, t0: float, script: list[tuple[float, str]]) -> None:
        for at, text in script:
            while not self._stop.is_set():
                remaining = t0 + float(at) - time.monotonic()
                if remaining <= 0:
                    break
                self._stop.wait(min(remaining, 0.01))
            if self._stop.is_set() or gen != self._boot_gen:
                return
            self._emit_quiet(text.encode("utf-8"))

    def _noise_loop(self) -> None:
        assert self.noise is not None
        interval = max(1, int(self.noise.get("interval_ms", 1000))) / 1000.0
        line = (str(self.noise.get("pattern", "noise")) + "\r\n").encode("utf-8")
        t_next = time.monotonic() + interval
        while not self._stop.is_set():
            remaining = t_next - time.monotonic()
            if remaining > 0:
                self._stop.wait(remaining)
                continue
            self._emit_quiet(line)
            t_next += interval

    def _maybe_relock(self) -> None:
        lock = self.console_lock
        if lock is None or self._state != "READY":
            return
        idle_s = float(lock.get("idle_s") or 0)
        if idle_s > 0 and self._last_tx and time.monotonic() - self._last_tx >= idle_s:
            self._state = "LOCKED"

    def _handle_line(self, state: str, line: bytearray) -> None:
        """P7 新增狀態（LOCKED／STEPS）的整行處理。"""
        text = line.decode("utf-8", errors="replace")
        line.clear()
        if state == "LOCKED":
            assert self.console_lock is not None
            if text == str(self.console_lock.get("secret", "")):
                self._state = "READY"
                self._respond(b"\r\n" + self.prompt.encode("utf-8"))
            return
        assert self.login_steps is not None
        step = self.login_steps[self._step]
        if text == str(step.get("expect", "")):
            self._last_prompt = str(step.get("respond", "")).encode("utf-8")
            self._step += 1
            if self._step >= len(self.login_steps):
                self._state = "READY"
            self._respond(self._last_prompt)
        elif self._last_prompt is not None:
            self._respond(self._last_prompt)

    # ------------------------------------------------------------------
    # 內部：狀態機
    # ------------------------------------------------------------------

    def _reader_main(self) -> None:
        self._run_loop()
        # release_slave：slave 無人開啟時 read 得 EIO、_run_loop 返回；持續重入直到 stop()。
        while self.release_slave and not self._stop.wait(0.05):
            self._run_loop(resume=True)

    def _initial_output(self) -> bytes:
        """開機輸出：boot_on_start 時由 boot() 腳本負責；LOCKED／STEPS（含 login_steps=[] 的 READY）靜默。"""
        if self.boot_on_start is not None:
            return b""
        if self.console_lock is not None or self.login_steps is not None:
            return self.boot_banner
        prompt_b = self.prompt.encode("utf-8")
        return self.boot_banner + (b"login: " if self.login else prompt_b)

    def _run_loop(self, resume: bool = False) -> None:
        assert self.master_fd is not None
        prompt_b = self.prompt.encode("utf-8")
        if not resume:
            boot = self._initial_output()
            if boot:
                try:
                    self._write_all(boot)
                except OSError:
                    return

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
                    self._maybe_relock()
                    self._last_tx = time.monotonic()

                for b in chunk:
                    byte = bytes([b])
                    if byte == b"\n" and prev_was_cr:
                        prev_was_cr = False
                        continue
                    prev_was_cr = (byte == b"\r")

                    with self._lock:
                        state = self._state
                    if state in ("LOCKED", "STEPS"):
                        if byte in (b"\r", b"\n"):
                            self._handle_line(state, line_buf)
                        else:
                            line_buf.append(b)
                    elif state == "LOGIN":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.user:
                                self._respond(b"\r\nPassword: ")
                                self._state = "PASSWORD"
                            else:
                                self._respond(b"\r\nlogin: ")
                        else:
                            line_buf.append(b)
                    elif state == "PASSWORD":
                        if byte in (b"\r", b"\n"):
                            line_str = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if line_str == self.password:
                                self._respond(b"\r\n" + prompt_b)
                                self._state = "READY"
                            else:
                                self._respond(b"\r\nLogin incorrect\r\nlogin: ")
                                self._state = "LOGIN"
                        else:
                            line_buf.append(b)
                    elif state == "READY":
                        if byte == b"\x03":
                            line_buf.clear()
                            self._cancel_pending_responses()
                            self._respond(b"^C\r\n" + prompt_b)
                        elif byte in (b"\r", b"\n"):
                            cmd = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            override = self.overrides.get(cmd)
                            if override is not None:
                                self._respond(str(override.get("response", "")).encode("utf-8"))
                                continue
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
        d = self.delay_s

        m = _SLEEP_RE.match(cmd)
        if m:
            self._schedule(echo, delay_s=d)
            self._schedule(prompt_b, delay_s=float(m.group(1)))
            return

        m = _YES_HEAD_RE.match(cmd)
        if m:
            n = int(m.group(1))
            payload = (b"y\n" * (n // 2 + 1))[:n]
            self._schedule(echo + payload + prompt_b, delay_s=d)
            return

        m = _PRINTF_RANGE_RE.match(cmd)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            lines = [f"M{i}\r\n".encode("ascii") for i in range(lo, hi + 1)] if lo <= hi else []
            self._schedule(echo, delay_s=d)
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
        self._schedule(out + prompt_b, delay_s=d)
