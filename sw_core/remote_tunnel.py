"""serialwrap `remote` 隧道便利層（純 CLI，daemon 零改動）。

外包給系統 ssh 建立 `-R`（expose）／`-L`（connect）隧道，background 常駐、
flock registry 管理、readiness 確認。設計見
docs/superpowers/specs/2026-07-19-remote-tunnel-cli-design.md。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass

# `[user@]host:port`——host 允許 ssh_config alias（不含 '@'/':' 的一段）。
_TARGET_RE = re.compile(r"^(?:(?P<user>[^@:]+)@)?(?P<host>[^@:]+):(?P<port>\d+)$")


class TunnelError(Exception):
    """攜帶 error_code 的隧道錯誤；不讓例外穿越 CLI 邊界。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TunnelSpec:
    role: str  # "expose" | "connect"
    ssh_target: str
    port: int
    local: int | None = None
    forward_src: str | None = None
    remote_socket: str | None = None
    via: str = "ssh"  # "ssh" | "autossh"
    ssh_opts: tuple[str, ...] = ()
    ready_timeout: float = 10.0


def parse_target(target: str) -> tuple[str, int]:
    m = _TARGET_RE.match(target or "")
    if not m:
        raise TunnelError("INVALID_TARGET", f"target 需為 [user@]host:port，取得 {target!r}")
    port = int(m.group("port"))
    if not (1 <= port <= 65535):
        raise TunnelError("INVALID_TARGET", f"port 超出範圍：{port}")
    user = m.group("user")
    host = m.group("host")
    ssh_target = f"{user}@{host}" if user else host
    return ssh_target, port


def compute_identity(spec: TunnelSpec) -> str:
    # canonical effective forward spec：涵蓋所有會改變入口／目的地的欄位。
    canonical = "|".join(
        str(x)
        for x in (
            spec.role,
            spec.ssh_target,
            spec.port,
            spec.local,
            spec.forward_src,
            spec.remote_socket,
            spec.via,
            ";".join(spec.ssh_opts),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def default_ssh_opts(control_path: str) -> list[str]:
    """readiness／安全預設 -o。置於使用者 --ssh-opt 之前：OpenSSH 取同鍵的第一個 -o，故這些預設為權威、使用者 --ssh-opt 無法覆寫（BatchMode／ExitOnForwardFailure／ControlPath 等必須固定，刻意如此）。"""
    return [
        "-o", "BatchMode=yes",            # 禁互動認證，失敗即退出不卡死
        "-o", "ExitOnForwardFailure=yes",  # forward 建不起即退出，供 readiness 判死
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path}",
        "-o", "ControlPersist=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]


def _direction_args(spec: TunnelSpec) -> list[str]:
    """組裝 -R 或 -L 與其隧道規格（bind:destination）。"""
    if spec.role == "expose":
        if spec.remote_socket:
            bind = spec.remote_socket                      # 遠端 unix socket（硬化）
        else:
            bind = f"127.0.0.1:{spec.port}"                # 顯式遠端 loopback bind
        return ["-R", f"{bind}:{spec.forward_src}"]
    # connect：顯式本機 loopback bind
    local = spec.local if spec.local is not None else spec.port
    dest = spec.remote_socket if spec.remote_socket else f"localhost:{spec.port}"
    return ["-L", f"127.0.0.1:{local}:{dest}"]


def build_argv(spec: TunnelSpec, control_path: str) -> list[str]:
    """完整 ssh/autossh argv（含方向 -R/-L、預設 -o、--ssh-opt 透傳、target）。"""
    base = ["autossh", "-M", "0"] if spec.via == "autossh" else ["ssh"]
    argv = [*base, "-N", "-T", *default_ssh_opts(control_path), *spec.ssh_opts]
    argv += _direction_args(spec)
    argv.append(spec.ssh_target)
    return argv


def read_pid_start_ticks(pid: int) -> int | None:
    """Linux /proc/<pid>/stat 第 22 欄（starttime，clock ticks）。非 Linux／不存在回 None。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # comm 欄可能含空白/括號，故從最後一個 ')' 之後切。
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 2:].split()
    # 切點後的 fields[0] 為 state(第3欄)，starttime 為第22欄 → index 22-3 = 19。
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def pid_alive(pid: int, start_ticks: int | None) -> bool:
    """檢測 PID 是否存活，並以 start_ticks 防 PID 重用。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if start_ticks is None:
        return True  # 無 /proc（非 Linux）→ best-effort pid-only
    current = read_pid_start_ticks(pid)
    if current is None:
        return True
    return current == start_ticks


_POLL_INTERVAL_S = 0.2


def wait_ready(
    spec: TunnelSpec,
    pid: int,
    start_ticks: int | None,
    *,
    ssh_check,          # () -> bool：master 連線＋forward 建立
    role_probe,         # () -> bool：-R 遠端 bind loopback；-L health.ping。可 raise TunnelError
    sleep,              # (float) -> None
    monotonic,          # () -> float
    alive,              # () -> bool：行程是否仍活
    stderr_tail,        # () -> str：spawn log 的 stderr 尾（供 TUNNEL_SPAWN_FAILED）
) -> str:
    """回 'active'（就緒）或 'starting'（逾時但行程仍活）；行程死亡 → TUNNEL_SPAWN_FAILED。

    role_probe 可 raise TunnelError（例如 REMOTE_BIND_UNVERIFIED），會直接傳播。
    """
    deadline = monotonic() + spec.ready_timeout
    while True:
        if not alive():
            # 行程已死亡，取得 stderr 尾並拋出錯誤
            tail = (stderr_tail() or "").strip()
            # 僅保留最後 500 字元（避免長文本）
            if len(tail) > 500:
                tail = tail[-500:]
            raise TunnelError("TUNNEL_SPAWN_FAILED", tail)
        if ssh_check() and role_probe():   # role_probe 可 raise（REMOTE_BIND_UNVERIFIED）
            return "active"
        if monotonic() >= deadline:
            return "starting"
        sleep(_POLL_INTERVAL_S)


class Registry:
    """`<run_dir>/remote/` 下的 per-tunnel state JSON + flock 序列化。"""

    def __init__(self, run_dir: str) -> None:
        self.run_dir = run_dir
        self.remote_dir = os.path.join(run_dir, "remote")

    def _ensure_dir(self) -> None:
        """確保遠端目錄存在。"""
        os.makedirs(self.remote_dir, exist_ok=True)

    @contextlib.contextmanager
    def lock(self):
        """Exclusive flock 保護同步讀寫。"""
        self._ensure_dir()
        lock_path = os.path.join(self.remote_dir, ".registry.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def control_path(self, listen_port: int) -> str:
        """控制端點路徑（ssh -o ControlPath）。"""
        return os.path.join(self.remote_dir, f"cm-{listen_port}")

    def state_path(self, listen_port: int) -> str:
        """狀態檔路徑。"""
        return os.path.join(self.remote_dir, f"{listen_port}.json")

    def write(self, state: dict) -> None:
        """原子寫入狀態（先 tmp 再 replace）。"""
        self._ensure_dir()
        path = self.state_path(int(state["listen_port"]))
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, path)  # atomic

    def read(self, listen_port: int) -> dict | None:
        """讀取單一隧道狀態，不存在回 None。"""
        try:
            with open(self.state_path(listen_port), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def read_all(self) -> list[dict]:
        """讀取全部隧道狀態。"""
        out: list[dict] = []
        try:
            names = os.listdir(self.remote_dir)
        except OSError:
            return out
        for name in sorted(names):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(self.remote_dir, name), "r", encoding="utf-8") as fh:
                        out.append(json.load(fh))
                except (OSError, json.JSONDecodeError):
                    continue
        return out

    def remove(self, listen_port: int) -> None:
        """刪除隧道狀態與控制端點。"""
        for p in (self.state_path(listen_port), self.control_path(listen_port)):
            with contextlib.suppress(OSError):
                os.unlink(p)
