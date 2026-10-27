"""Slice 2.1 黑箱 harness（P5-T15；Tasks 20～22 擴充）。

以受測 artifact（`bb_harness.daemon_prefix()`／`cli_prefix()`，由 AB_ENV 選擇 Rust 或 Python 基線）啟動一個
隔離的 daemon，COM0 接到 PTY 假 DUT（`bb_faketarget.FakeTarget`），提供 CLI／NDJSON RPC／WAL 讀取等黑箱操作。

契約（與 regression ThrowawayDaemon 相同）：
- 環境以 `bb_harness.isolated_env(root)` 為底，另設 `SERIALWRAP_BY_ID_DIR=<root>/by-id`（只含 `fake-uart0`
  → FakeTarget slave）、`SERIALWRAP_BY_PATH_DIR=<root>/by-path`（空）、`SERIALWRAP_PROFILE_DIR=<root>/profiles`；
- daemon 以**無參數**啟動，stdout 丟棄、stderr 寫入 `<root>/daemon.stderr`，等 endpoint 可回 `health.ping`；
- `reuse_dirs=True` 沿用同一 `root` 的 state／wal／config，並建新的 FakeTarget 接到同一 by-id 名稱。

AF_UNIX 路徑上限約 108 bytes：POSIX 上 `SERIALWRAP_RUN_DIR` 一律放在短路徑暫存目錄（不放 `root`），stop 時清除。

Task 15a 相容：`login=False`／`boot_banner` 需要 Task 15a 擴充版 FakeTarget；擴充尚未落地時，`login=False`
且無 banner 者退回 P3 版（開機送 `login: `），由 profile 的 `user_env`／`pass_env`（harness 一律提供帳密）完成登入，
外部可觀察的終態（COM0 READY）相同；需要 banner 而擴充未落地即拋 RuntimeError。

stdlib＋pytest＋PyYAML，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from bb_faketarget import FakeTarget
from bb_harness import CliResult, cli_prefix, daemon_prefix, isolated_env, run_cli

BY_ID_NAME = "fake-uart0"
DEVICE_PLACEHOLDER = "__DEVICE_BY_ID__"
CRED_USER_ENV = "SW_S21_USER"
CRED_PASS_ENV = "SW_S21_PASS"
CRED_USER = "root"
CRED_PASS = "pw"

# 單一 COM0、platform shell、prompt `(?m)^root@t:.*# `；device_by_id 佔位由 start_daemon 換成 by-id 連結路徑。
SHELL_TARGET_YAML: str = (
    "profiles:\n"
    "  t:\n"
    "    platform: shell\n"
    "    prompt_regex: '(?m)^root@t:.*# '\n"
    "    login_regex: '(?mi)^.*login:\\s*$'\n"
    "    password_regex: '(?mi)^password:\\s*$'\n"
    f"    user_env: {CRED_USER_ENV}\n"
    f"    pass_env: {CRED_PASS_ENV}\n"
    "    timeout_s: 10\n"
    "targets:\n"
    "  - profile: t\n"
    "    com: COM0\n"
    f"    device_by_id: {DEVICE_PLACEHOLDER}\n"
)


def _faketarget_kwargs() -> set[str]:
    try:
        return set(inspect.signature(FakeTarget.__init__).parameters)
    except (TypeError, ValueError):
        return set()


class _DaemonTarget(FakeTarget):
    """daemon 相容的 FakeTarget 變體（同 test_bb_daemon_e2e 的既有做法）。

    連結建立後立即關閉 slave fd：daemon 的外部 holder 偵測會把本行程持有的 slave 視為外部 holder。
    reader 在 daemon 關閉或重開 slave 造成的 EIO 後繼續存活，等待下一次開啟（release／attach 循環）。
    """

    def start(self, by_id_dir: Path, name: str = BY_ID_NAME) -> Path:
        link = super().start(by_id_dir, name)
        if self.slave_fd is not None:
            os.close(self.slave_fd)
            self.slave_fd = None
        return link

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            super()._run_loop()
            if self._stop.wait(0.05):
                break


def _make_target(workdir: Path, *, prompt: str, login: bool, boot_banner: bytes) -> FakeTarget:
    params = _faketarget_kwargs()
    kwargs: dict[str, Any] = {"prompt": prompt, "user": CRED_USER, "password": CRED_PASS}
    if "login" in params:
        kwargs["login"] = login
    if "boot_banner" in params:
        kwargs["boot_banner"] = boot_banner
    elif boot_banner:
        raise RuntimeError("boot_banner 需要 Task 15a 擴充版 FakeTarget（bb_faketarget 尚未支援）")
    return _DaemonTarget(workdir, **kwargs)


def _endpoint_connect(endpoint: str, timeout: float) -> socket.socket:
    if endpoint.startswith("tcp://"):
        host, port = endpoint[len("tcp://"):].rsplit(":", 1)
        return socket.create_connection((host, int(port)), timeout=timeout)
    path = endpoint[len("unix://"):] if endpoint.startswith("unix://") else endpoint
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        raise
    return sock


def rpc_call(endpoint: str, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    """送一行 NDJSON 請求並讀回第一行回應；連線／解析失敗直接拋例外。"""
    req = json.dumps({"id": 1, "method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    with _endpoint_connect(endpoint, timeout) as sock:
        sock.sendall(req + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        raise ConnectionError(f"{method}: daemon 未回應")
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{method}: 回應不是 object：{obj!r}")
    return obj


class DaemonStartError(RuntimeError):
    """daemon 在 endpoint 就緒前即結束（`rc`＝exit code、`stderr`＝`<root>/daemon.stderr` 全文）。"""

    def __init__(self, rc: int, stderr: str) -> None:
        super().__init__(f"daemon 啟動即結束 rc={rc}；stderr 尾段：{stderr[-2000:]!r}")
        self.rc = rc
        self.stderr = stderr


def prepare_root(root: Path, *, state_json: bytes | str | None = None) -> Path:
    """預先建立 `root` 的隔離目錄（state／wal／config…），可選擇寫入 `state/state.json`。

    之後以 `start_daemon(root, reuse_dirs=True)` 啟動即沿用該 state.json（啟動期 state 契約測試用）。
    """
    root = Path(root)
    isolated_env(root)
    if state_json is not None:
        data = state_json.encode("utf-8") if isinstance(state_json, str) else state_json
        (root / "state" / "state.json").write_bytes(data)
    return root


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _expected_exe(prefix: list[str]) -> tuple[str, str | None]:
    """回傳 (預期 /proc/<pid>/exe 實體路徑, cmdline 必含的腳本名或 None)。

    Python 基線的 daemon_prefix 為 shell wrapper（exec python3 serialwrapd.py）或 [python, serialwrapd.py]：
    行程 exe 為直譯器、cmdline 必含 serialwrapd.py。其餘（Rust artifact）exe 即 prefix[0] 本身。
    """
    first = prefix[0]
    if len(prefix) >= 2 and prefix[1].endswith(".py"):
        return os.path.realpath(first), "serialwrapd.py"
    try:
        with open(first, "rb") as fp:
            head = fp.read(2)
    except OSError:
        head = b""
    if head == b"#!":
        py = shutil.which("python3")
        return os.path.realpath(py) if py else "", "serialwrapd.py"
    return os.path.realpath(first), None


def check_artifact_identity(pid: int, prefix: list[str]) -> None:
    """斷言 pid 實際執行的是所選 daemon artifact（防止 Rust 輪靜默跑 Python）。

    Linux 讀 `/proc/<pid>/exe`；無 /proc 的平台（Windows）不檢查（LOCAL-WINDOWS-TODO：QueryFullProcessImageName）。
    """
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.is_dir():
        return
    exe = os.path.realpath(proc_dir / "exe")
    want, script = _expected_exe(prefix)
    cmdline = (proc_dir / "cmdline").read_bytes().split(b"\0")
    ok = exe == want
    if script is not None:
        ok = ok and any(arg.endswith(script.encode()) for arg in cmdline)
    if not ok:
        import pytest

        pytest.fail(
            f"daemon artifact 身分不符：exe={exe!r} cmdline={cmdline!r}；預期 exe={want!r}"
            + (f"＋cmdline 含 {script}" if script else "")
        )


@dataclasses.dataclass
class Slice21Daemon:
    root: Path
    env: dict[str, str]
    endpoint: str                      # POSIX socket 路徑；Windows "tcp://127.0.0.1:<port>"
    state_dir: Path
    wal_dir: Path
    targets: dict[str, FakeTarget]
    proc: subprocess.Popen
    cli: str
    daemon_cmd: list[str] = dataclasses.field(default_factory=list)
    cli_cmd: list[str] = dataclasses.field(default_factory=list)
    run_dir: Path | None = None
    _rc: int | None = None

    def sw(self, *args: str, timeout: float = 30.0) -> dict:
        """`serialwrap --socket <endpoint> <args>`；回 stdout JSON 物件＋`_rc`＋`_stderr`。"""
        argv = ["--socket", self.endpoint, *args]
        if self.cli_cmd and self.cli_cmd != cli_prefix():
            p = subprocess.run([*self.cli_cmd, *argv], env=self.env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout)
            r = CliResult(rc=p.returncode, stdout=p.stdout or b"", stderr=p.stderr or b"")
        else:
            r = run_cli(argv, env=self.env, timeout_s=timeout)
        text = r.stdout.decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(text) if text else {}
        except json.JSONDecodeError:
            obj = {"_raw": text}
        if not isinstance(obj, dict):
            obj = {"_raw": obj}
        obj["_rc"] = r.rc
        obj["_stderr"] = r.stderr.decode("utf-8", errors="replace")
        return obj

    def rpc(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        return rpc_call(self.endpoint, method, params, timeout)

    def _session(self, com: str) -> dict | None:
        obj = self.sw("session", "list")
        for row in obj.get("sessions") or []:
            if isinstance(row, dict) and row.get("com") == com:
                return row
        return None

    def wait_state(self, com: str, state: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            last = self._session(com)
            if last is not None and last.get("state") == state:
                return last
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise AssertionError(f"{com} 未在 {timeout}s 內進入 {state}；最後觀察：{last!r}；stderr 尾段：{self._stderr_tail()!r}")

    def wait_command_done(self, cmd_id: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            obj = self.sw("cmd", "status", "--cmd-id", cmd_id)
            last = obj.get("command")
            if isinstance(last, dict) and last.get("done_at"):
                return last
            time.sleep(0.2)
        raise AssertionError(f"命令 {cmd_id} 未在 {timeout}s 內完成；最後觀察：{last!r}")

    def wal_records(self) -> list[dict]:
        path = self.wal_dir / "raw.wal.ndjson"
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def _stderr_tail(self, n: int = 2000) -> str:
        try:
            return (self.root / "daemon.stderr").read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""

    def stop(self, timeout: float = 15.0) -> int:
        """`daemon.stop` → 等行程結束 → 回 exit code（冪等；逾時則強制終止並回其 exit code）。"""
        if self._rc is not None:
            return self._rc
        proc = self.proc
        if proc.poll() is None:
            try:
                self.rpc("daemon.stop", {}, timeout=2.0)
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
        self._rc = proc.returncode if proc.returncode is not None else -1
        for t in self.targets.values():
            t.stop()
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)
        return self._rc


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3.0)
            return
        except subprocess.TimeoutExpired:
            continue


def start_daemon(
    root: Path,
    *,
    daemon: list[str] | None = None,
    cli: list[str] | None = None,
    profile_yaml: str = SHELL_TARGET_YAML,
    prompt: str = "root@t:/# ",
    login: bool = False,
    boot_banner: bytes = b"",
    extra_env: dict[str, str] | None = None,
    reuse_dirs: bool = False,
    ready_timeout: float = 20.0,
) -> Slice21Daemon:
    """啟動受測 daemon（預設 `daemon_prefix()`）＋COM0 FakeTarget；回傳 `Slice21Daemon`。"""
    root = Path(root)
    if not reuse_dirs and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    daemon_cmd = list(daemon) if daemon else daemon_prefix()
    cli_cmd = list(cli) if cli else cli_prefix()

    run_dir: Path | None = None
    overrides: dict[str, str] = {
        "SERIALWRAP_PROFILE_DIR": str(root / "profiles"),
        CRED_USER_ENV: CRED_USER,
        CRED_PASS_ENV: CRED_PASS,
    }
    if os.name == "nt":
        port = _free_tcp_port()
        overrides["SERIALWRAP_TCP_PORT"] = str(port)
        endpoint = f"tcp://127.0.0.1:{port}"
    else:
        run_dir = Path(tempfile.mkdtemp(prefix="sw21-", dir="/tmp" if os.path.isdir("/tmp") else None))
        overrides["SERIALWRAP_RUN_DIR"] = str(run_dir)
        endpoint = str(run_dir / "serialwrapd.sock")
    overrides.update(extra_env or {})
    env = isolated_env(root, **overrides)

    by_id_dir = Path(env["SERIALWRAP_BY_ID_DIR"])
    n = sum(1 for p in root.iterdir() if p.name.startswith("fw"))
    target = _make_target(root / f"fw{n}", prompt=prompt, login=login, boot_banner=boot_banner)
    link = target.start(by_id_dir, BY_ID_NAME)

    profile_dir = root / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "slice21.yaml").write_text(profile_yaml.replace(DEVICE_PLACEHOLDER, str(link)), encoding="utf-8")

    stderr_path = root / "daemon.stderr"
    with open(stderr_path, "ab") as stderr_file:
        proc = subprocess.Popen(
            daemon_cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            start_new_session=(os.name == "posix"),
        )
    d = Slice21Daemon(
        root=root,
        env=env,
        endpoint=endpoint,
        state_dir=Path(env["SERIALWRAP_STATE_DIR"]),
        wal_dir=Path(env["SERIALWRAP_WAL_DIR"]),
        targets={"COM0": target},
        proc=proc,
        cli=" ".join(cli_cmd),
        daemon_cmd=daemon_cmd,
        cli_cmd=cli_cmd,
        run_dir=run_dir,
    )
    deadline = time.monotonic() + ready_timeout
    while True:
        if proc.poll() is not None:
            rc = d.stop()
            raise DaemonStartError(rc, d._stderr_tail(1 << 20))
        try:
            if d.rpc("health.ping", {}, timeout=0.5).get("ok"):
                break
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            d.stop()
            raise TimeoutError(f"daemon 未在 {ready_timeout}s 內回應 health.ping；stderr 尾段：{d._stderr_tail()!r}")
        time.sleep(0.1)
    try:
        check_artifact_identity(proc.pid, daemon_cmd)
    except BaseException:
        d.stop()
        raise
    return d
