"""serialwrap `remote` 隧道便利層（純 CLI，daemon 零改動）。

外包給系統 ssh 建立 `-R`（expose）／`-L`（connect）隧道，background 常駐、
flock registry 管理、readiness 確認。設計見
docs/superpowers/specs/2026-07-19-remote-tunnel-cli-design.md。
"""
from __future__ import annotations

import hashlib
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
    """readiness／安全預設 -o（可被使用者 --ssh-opt 覆寫，故置於其前）。"""
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
