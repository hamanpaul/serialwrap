"""PTY 假 DUT。

使用 pty.openpty() 與獨立 thread 狀態機模擬 DUT（開機 prompt、login、password、prompt、echo）。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。

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
"""
from __future__ import annotations

import os
import select
import threading
import time
from pathlib import Path


class FakeTarget:
    def __init__(
        self,
        workdir: Path,
        *,
        prompt: str = "root@fake:~# ",
        user: str = "root",
        password: str = "pw",
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
        self.login_steps = None if login_steps is None else [dict(st) for st in login_steps]
        self.overrides: dict = dict((commands or {}).get("overrides") or {})
        self.delay_s = max(0, int(delay_ms)) / 1000.0
        self.noise = dict(noise) if noise else None
        self.console_lock = dict(console_lock) if console_lock else None
        self.boot_on_start = list(boot_on_start) if boot_on_start is not None else None
        self.release_slave = release_slave
        self._write_lock = threading.Lock()
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
        self._thread_exc: BaseException | None = None

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

        by_id_dir = Path(by_id_dir)
        by_id_dir.mkdir(parents=True, exist_ok=True)
        self.link_path = by_id_dir / name
        if self.link_path.is_symlink() or self.link_path.exists():
            self.link_path.unlink()
        os.symlink(self.slave_path, self.link_path)

        if self.release_slave:
            os.close(self.slave_fd)
            self.slave_fd = None

        self._stop.clear()
        self._reset_login_state()
        self._thread = threading.Thread(target=self._reader_main, daemon=True)
        self._thread.start()
        if self.noise:
            self._spawn(self._noise_loop)
        if self.boot_on_start is not None:
            self.boot(self.boot_on_start)
        return self.link_path

    # ── P7 擴充 ────────────────────────────────────────────────────────────

    def _initial_state(self) -> str:
        if self.console_lock is not None:
            return "LOCKED"
        if self.login_steps is not None:
            return "STEPS" if self.login_steps else "READY"
        return "LOGIN"

    def _reset_login_state(self) -> None:
        self._state = self._initial_state()
        self._step = 0
        self._last_prompt = None

    def _spawn(self, fn, *args) -> None:
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._aux_threads.append(t)
        t.start()

    def _write(self, data: bytes) -> None:
        """所有輸出的唯一出口：freeze 期間丟棄；跨 thread 以鎖序列化。"""
        with self._write_lock:
            if self._frozen or self.master_fd is None:
                return
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def _respond(self, data: bytes) -> None:
        if self.delay_s:
            time.sleep(self.delay_s)
        self._write(data)

    def emit(self, text: str | bytes) -> None:
        """立即輸出（str 以 UTF-8 編碼）。"""
        self._write(text.encode("utf-8") if isinstance(text, str) else bytes(text))

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

    def _boot_loop(self, gen: int, t0: float, script: list[tuple[float, str]]) -> None:
        for at, text in script:
            while not self._stop.is_set():
                remaining = t0 + float(at) - time.monotonic()
                if remaining <= 0:
                    break
                self._stop.wait(min(remaining, 0.01))
            if self._stop.is_set() or gen != self._boot_gen:
                return
            self.emit(text)

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
            self._write(line)
            t_next += interval

    def rx_mark(self) -> int:
        """目前 rx_log 長度，供 rx_log_since() 取之後收到的 TX。"""
        with self._lock:
            return len(self._rx_log)

    def rx_log_since(self, mark: int) -> bytes:
        return self.rx_log()[mark:]

    def _maybe_relock(self) -> None:
        lock = self.console_lock
        if lock is None or self._state != "READY":
            return
        idle_s = float(lock.get("idle_s") or 0)
        if idle_s > 0 and self._last_tx and time.monotonic() - self._last_tx >= idle_s:
            self._state = "LOCKED"

    def _handle_line(self, state: str, line: bytearray) -> None:
        """P7 新增狀態（LOCKED／STEPS）與 READY override 的整行處理。"""
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

    def _reader_main(self) -> None:
        self._run_loop()
        # release_slave：slave 無人開啟時 read 得 EIO、_run_loop 返回；持續重入直到 stop()。
        while self.release_slave and not self._stop.wait(0.05):
            self._run_loop(resume=True)

    def _run_loop(self, resume: bool = False) -> None:
        assert self.master_fd is not None
        if not resume and self._state == "LOGIN" and self.boot_on_start is None:
            try:
                os.write(self.master_fd, b"login: ")
            except OSError:
                return

        line_buf = bytearray()
        prev_was_cr = False

        try:
            while not self._stop.is_set():
                try:
                    rlist, _, _ = select.select([self.master_fd], [], [], 0.05)
                except (OSError, ValueError):
                    break
                if not rlist:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096)
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
                                self._respond(b"\r\n" + self.prompt.encode("utf-8"))
                                self._state = "READY"
                            else:
                                self._respond(b"\r\nLogin incorrect\r\nlogin: ")
                                self._state = "LOGIN"
                        else:
                            line_buf.append(b)
                    elif state == "READY":
                        if byte == b"\x03":
                            line_buf.clear()
                            self._respond(b"^C\r\n" + self.prompt.encode("utf-8"))
                        elif byte in (b"\r", b"\n"):
                            cmd = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            override = self.overrides.get(cmd)
                            if override is not None:
                                self._respond(str(override.get("response", "")).encode("utf-8"))
                                continue
                            out = (cmd + "\r\n").encode("utf-8")
                            if cmd.startswith("echo "):
                                val = cmd[5:]
                                out += (val + "\r\n").encode("utf-8")
                            elif cmd == "echo":
                                out += b"\r\n"
                            out += self.prompt.encode("utf-8")
                            self._respond(out)
                        else:
                            line_buf.append(b)
        except OSError:
            pass
        except BaseException as e:
            self._thread_exc = e

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

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        for t in self._aux_threads:
            t.join(timeout=2.0)
        self._aux_threads.clear()
        for fd in (self.master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master_fd = None
        self.slave_fd = None
        if self.link_path and self.link_path.is_symlink():
            try:
                self.link_path.unlink()
            except OSError:
                pass
        self.link_path = None
