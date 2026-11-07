"""以真實 `/bin/sh` 扮演 DUT 的 PTY 假板（P8 Task 0；計畫 09 Task 0 Interfaces）。

`bb_faketarget.FakeTarget` 只模擬 echo／sleep／yes／printf，沒有 base64／md5sum／mv，file.push／pull 黑箱因此需要本模組：

    daemon ──(slave A，當 UART)── PTY pair A ──master A── [relay thread] ──master B── PTY pair B ── /bin/sh -i

- PTY pair A：本程序只持有 master A（slave A 建立 by-id 連結前即關閉，避免被 daemon 的外部 holder 偵測當成持有者）；
  slave A 無人開啟時 master A 讀取得 EIO，每 0.02 s 重試；此時 shell 的輸出直接丟棄（如同沒人接的實體 UART）。
- PTY pair B：`setsid -c /bin/sh -i`（B 為 shell 的 controlling tty，`^C` 可產生 SIGINT）；`PS1='root@sw-sim:/# '`、
  `HOME`／`TMPDIR`／cwd 一律指向案例暫存目錄 `root`、`ENV` 不設定，shell 不讀寫真實家目錄。
- 回顯（echo）由 relay 產生：B 的 termios 關閉 ECHO，relay 每轉送一個輸入 byte 就回顯（`\n` 回顯為 `\r\n`），
  因此 `echo_stall_after` 能精確在第 N 個輸入 byte 後停止回顯；shell 的輸出（prompt、命令結果）照常經 B 的 ONLCR 回到 daemon。
- `rx_log()`：master A 實際收到的全部 bytes（DUT 端、shell 輸入側的外部 oracle，Review Focus 6）；`commands()` 依 `\n` 切出完整輸入行。

選項（全部 keyword-only；計數一律以 `arm()` 呼叫時或啟動時為起點，單位為輸入 bytes——file transfer 命令皆為 ASCII，與字元數相同）：
- `line_limit`：每行最多轉送／回顯 N 個 byte 給 shell，超出部分丟棄直到 `\n`（模擬 prpl 之類 console 行長限制）。
- `hide_tools`：PATH 前置目錄放同名腳本（stderr 印 `sh: <tool>: not found`、exit 127），模擬缺 decoder／encoder。
- `echo_stall_after`：轉送 N 個輸入 byte 後停止回顯（命令照常執行、輸出照常回傳）。
- `silent_after`：收到 N 個輸入 byte 後 DUT 完全靜默（不再轉送給 shell、不回顯、不輸出；`rx_log` 照記）。
- `half_prompt`：收到含此子字串的輸入行後，shell 下一個 prompt 只回前半段，之後完全靜默。
- `slow_reader_bps`：以 `bps/10` bytes/s 限速讀取 master A（8N1），對 daemon 形成寫入背壓。

POSIX 限定（pty／termios；需要 util-linux `setsid`）。stdlib only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import errno
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

PROMPT = "root@sw-sim:/# "
# 寫回 daemon 的待送緩衝上限（daemon 長時間不讀時丟棄最舊的部分，避免無界成長）
_TO_DUT_MAX = 4 * 1024 * 1024


def open_dut_pty() -> tuple[int, str]:
    """建立 DUT 端 PTY pair：slave 設 raw 後關閉，回傳 (非阻塞 master fd, slave 路徑)。"""
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        path = os.ttyname(slave)
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    return master, path


def _session_members(sid: int) -> list[int]:
    """`/proc/*/stat` 的 session 欄等於 `sid` 的行程（shell 與其所有子行程）。"""
    out: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[3]) == sid:
                out.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return out


class ShellTarget:
    def __init__(
        self,
        root: Path,
        *,
        by_id_name: str = "dut",
        line_limit: int | None = None,
        hide_tools: tuple[str, ...] | list[str] = (),
        echo_stall_after: int | None = None,
        silent_after: int | None = None,
        half_prompt: str | bytes | None = None,
        slow_reader_bps: int | None = None,
        prompt: str = PROMPT,
    ) -> None:
        if line_limit is not None and line_limit < 1:
            raise ValueError("line_limit 必須 ≥1")
        if slow_reader_bps is not None and slow_reader_bps < 10:
            raise ValueError("slow_reader_bps 必須 ≥10")
        self.root = Path(root)
        self.by_id_name = by_id_name
        self.prompt = prompt
        self.line_limit = line_limit
        self.hide_tools = tuple(hide_tools)
        self.slow_reader_bps = slow_reader_bps
        self.slave_path: str = ""
        self._lock = threading.Lock()
        self._rx = bytearray()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._a: int | None = None
        self._b: int | None = None
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        self.arm(echo_stall_after=echo_stall_after, silent_after=silent_after, half_prompt=half_prompt)

    # ------------------------------------------------------------------ 控制
    def arm(
        self,
        *,
        echo_stall_after: int | None = None,
        silent_after: int | None = None,
        half_prompt: str | bytes | None = None,
    ) -> None:
        """自此刻起重新計數並套用三個故障注入選項（None＝關閉該選項）；已進入的靜默狀態一併解除。"""
        hp = half_prompt.encode("utf-8") if isinstance(half_prompt, str) else half_prompt
        with self._lock:
            self._echo_stall_after = echo_stall_after
            self._silent_after = silent_after
            self._half_prompt = hp
            self._fwd_count = 0
            self._in_count = 0
            self._silent = False
            self._half_pending = False
            self._line_buf = bytearray()
            self._line_len = 0
            self._carry = bytearray()

    def start(self) -> "ShellTarget":
        if os.name != "posix":
            raise RuntimeError("ShellTarget 僅支援 POSIX 平台")
        import pty
        import termios

        setsid = shutil.which("setsid")
        if setsid is None:
            raise RuntimeError("ShellTarget 需要 util-linux setsid（設定 shell 的 controlling tty）")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)
        path_dirs = []
        if self.hide_tools:
            hide = self.root / ".sw-hidebin"
            hide.mkdir(exist_ok=True)
            for tool in self.hide_tools:
                script = hide / tool
                script.write_text(f'#!/bin/sh\necho "sh: {tool}: not found" >&2\nexit 127\n', encoding="utf-8")
                script.chmod(0o755)
            path_dirs.append(str(hide))
        path_dirs += ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]

        self._a, self.slave_path = open_dut_pty()
        b_master, b_slave = pty.openpty()
        try:
            attrs = termios.tcgetattr(b_slave)
            attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
            termios.tcsetattr(b_slave, termios.TCSANOW, attrs)
            env = {
                "HOME": str(self.root),
                "TMPDIR": str(self.root / "tmp"),
                "PWD": str(self.root),
                "PATH": ":".join(path_dirs),
                "PS1": self.prompt,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TERM": "dumb",
            }
            self._proc = subprocess.Popen(
                [setsid, "-c", "/bin/sh", "-i"],
                stdin=b_slave,
                stdout=b_slave,
                stderr=b_slave,
                cwd=str(self.root),
                env=env,
                close_fds=True,
            )
        except BaseException:
            os.close(b_master)
            os.close(self._a)
            self._a = None
            raise
        finally:
            os.close(b_slave)
        os.set_blocking(b_master, False)
        self._b = b_master
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="shell-target", daemon=True)
        self._thread.start()
        return self

    def link(self, by_id_dir: Path) -> Path:
        """在 `by_id_dir` 建立 `by_id_name` → slave A 的 symlink（已存在則取代）。"""
        link = Path(by_id_dir) / self.by_id_name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(self.slave_path, link)
        return link

    # ------------------------------------------------------------------ oracle
    def rx_log(self) -> bytes:
        with self._lock:
            return bytes(self._rx)

    def commands(self) -> list[str]:
        """`rx_log()` 依 `\\n` 切出的完整輸入行（去掉行尾 `\\r`；最後一段未以 `\\n` 結尾者不列入）。"""
        parts = self.rx_log().split(b"\n")[:-1]
        return [p.decode("utf-8", errors="replace").rstrip("\r") for p in parts]

    def shell_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ relay
    def _process_input(self, data: bytes, to_shell: bytearray, to_dut: bytearray) -> None:
        """daemon → DUT 的 bytes：記錄、套用故障注入、轉送給 shell 並產生回顯（持鎖呼叫）。"""
        self._rx += data
        for i in range(len(data)):
            byte = data[i:i + 1]
            self._in_count += 1
            if self._silent:
                continue
            if self._silent_after is not None and self._in_count > self._silent_after:
                self._silent = True
                continue
            newline = byte == b"\n"
            if self.line_limit is not None and not newline and self._line_len >= self.line_limit:
                continue
            to_shell += byte
            self._fwd_count += 1
            if self._echo_stall_after is None or self._fwd_count <= self._echo_stall_after:
                to_dut += b"\r\n" if newline else byte
            if newline:
                if self._half_prompt is not None and self._half_prompt in self._line_buf:
                    self._half_pending = True
                self._line_buf.clear()
                self._line_len = 0
            else:
                self._line_len += 1
                if len(self._line_buf) < 65536:
                    self._line_buf += byte

    def _process_output(self, data: bytes, to_dut: bytearray) -> None:
        """shell → daemon 的 bytes（持鎖呼叫）。"""
        if self._silent:
            return
        if self._half_pending:
            prompt = self.prompt.encode("utf-8")
            data = bytes(self._carry) + data
            self._carry.clear()
            i = data.find(prompt)
            if i < 0:
                # prompt 可能跨兩次 read：結尾若是 prompt 的前綴就先扣住，與下一段合併再找
                keep = next((k for k in range(min(len(prompt) - 1, len(data)), 0, -1)
                             if prompt.startswith(data[-k:])), 0)
                to_dut += data[: len(data) - keep]
                self._carry += data[len(data) - keep:]
                return
            to_dut += data[: i + max(1, len(prompt) // 2)]
            self._half_pending = False
            self._silent = True
            return
        to_dut += data

    def _run(self) -> None:
        a, b, wake = self._a, self._b, self._wake_r
        assert a is not None and b is not None and wake is not None
        to_shell = bytearray()
        to_dut = bytearray()
        slave_open = False
        shell_dead = False
        bps = self.slow_reader_bps
        rate = (bps / 10.0) if bps else None
        tokens = 0.0
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if rate is not None:
                tokens = min(tokens + (now - last) * rate, max(1.0, rate / 10.0))
            last = now
            rlist = [wake]
            can_read_a = rate is None or tokens >= 1.0
            if can_read_a:
                rlist.append(a)
            if not shell_dead:
                rlist.append(b)
            wlist = []
            if to_shell and not shell_dead:
                wlist.append(b)
            if to_dut and slave_open:
                wlist.append(a)
            timeout = 0.2 if can_read_a else max(0.001, (1.0 - tokens) / rate)
            try:
                rr, ww, _ = select.select(rlist, wlist, [], timeout)
            except (OSError, ValueError):
                break
            if wake in rr or self._stop.is_set():
                break
            if a in rr:
                n = 4096 if rate is None else max(1, min(4096, int(tokens)))
                try:
                    data = os.read(a, n)
                except BlockingIOError:
                    data = b""
                    slave_open = True
                except OSError as e:
                    if e.errno != errno.EIO:
                        break
                    # slave A 無人開啟：沒人接的 UART，輸出丟棄
                    slave_open = False
                    to_dut.clear()
                    if self._stop.wait(0.02):
                        break
                    continue
                if data:
                    slave_open = True
                    if rate is not None:
                        tokens -= len(data)
                    with self._lock:
                        self._process_input(data, to_shell, to_dut)
            if b in rr and not shell_dead:
                try:
                    out = os.read(b, 4096)
                except BlockingIOError:
                    out = b""
                except OSError:
                    out = b""
                    shell_dead = True
                if out:
                    with self._lock:
                        self._process_output(out, to_dut)
            if b in ww and to_shell:
                try:
                    n = os.write(b, to_shell)
                    del to_shell[:n]
                except BlockingIOError:
                    pass
                except OSError:
                    shell_dead = True
                    to_shell.clear()
            if a in ww and to_dut:
                try:
                    n = os.write(a, to_dut)
                    del to_dut[:n]
                except BlockingIOError:
                    pass
                except OSError:
                    slave_open = False
                    to_dut.clear()
            if len(to_dut) > _TO_DUT_MAX:
                del to_dut[: len(to_dut) - _TO_DUT_MAX]

    # ------------------------------------------------------------------ 收尾
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
        proc, self._proc = self._proc, None
        # 先關 master B（hangup：SIGHUP 給 controlling process 與前景 job），再清掉同 session 的殘留行程
        for name in ("_b", "_a", "_wake_r", "_wake_w"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
        if proc is not None:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            for pid in _session_members(proc.pid) + ([proc.pid] if proc.poll() is None else []):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

    def __enter__(self) -> "ShellTarget":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["ShellTarget", "PROMPT", "open_dut_pty"]
