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
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .client import rpc_call as _rpc_call

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
    ssh_check: Callable[[], bool],          # master 連線＋forward 建立
    role_probe: Callable[[], bool],         # -R 遠端 bind loopback；-L health.ping。可 raise TunnelError
    sleep: Callable[[float], None],         # sleep(float)
    monotonic: Callable[[], float],         # 單調時鐘 epoch (float)
    alive: Callable[[], bool],              # 行程是否仍活
    stderr_tail: Callable[[], str],         # spawn log 的 stderr 尾（供 TUNNEL_SPAWN_FAILED）
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


def endpoint_for(spec: TunnelSpec) -> str:
    """回傳本機可探測的 endpoint（`-L`／direct 皆落在本機 loopback）。"""
    local = spec.local if spec.local is not None else spec.port
    return f"tcp://127.0.0.1:{local}"


def make_ssh_check(
    spec: TunnelSpec,
    control_path: str,
    *,
    runner: Callable[[list[str]], tuple[int, str]],
) -> Callable[[], bool]:
    """回傳 `() -> bool`：以既有 master 連線跑 `ssh -O check` 確認 control socket 存活。"""
    def _check() -> bool:
        rc, _ = runner(["ssh", "-O", "check", "-o", f"ControlPath={control_path}", spec.ssh_target])
        return rc == 0
    return _check


def make_role_probe(
    spec: TunnelSpec,
    control_path: str,
    endpoint: str,
    *,
    runner: Callable[[list[str]], tuple[int, str]],
    ping: Callable[[str], bool],
) -> Callable[[], bool]:
    """回傳 `() -> bool`：依 role 選擇對應 readiness 判定，可 raise TunnelError（REMOTE_BIND_UNVERIFIED）。

    - connect：以 `ping(endpoint)` 驗證本機端可用。
    - expose + remote_socket（unix）：遠端 bind 天然限定於該 socket 路徑，fail-closed 免驗證。
    - expose + tcp：借 master 連線在 relay 端跑 `ss` 驗遠端 bind 為 loopback；
      查不到／查失敗／wildcard bind 一律視為不安全，raise REMOTE_BIND_UNVERIFIED。
    """
    def _probe() -> bool:
        if spec.role == "connect":
            return bool(ping(endpoint))
        # expose：unix socket 模式天然 fail-closed，免 ss 驗證
        if spec.remote_socket:
            return True
        # tcp 模式：以 master 連線在 relay 跑 ss，驗遠端 bind 為 loopback（fail-closed）
        rc, out = runner([
            "ssh", "-o", f"ControlPath={control_path}", spec.ssh_target,
            "ss", "-ltnH", f"sport = :{spec.port}",
        ])
        if rc != 0 or not out.strip():
            raise TunnelError("REMOTE_BIND_UNVERIFIED", "無法在 relay 驗證遠端 bind")
        if not _bind_is_loopback_only(out, spec.port):
            raise TunnelError("REMOTE_BIND_UNVERIFIED", out.strip()[:200])
        return True
    return _probe


def _bind_is_loopback_only(ss_out: str, port: int) -> bool:
    """ss 輸出中該 port 的所有 bind 位址是否皆為 loopback（allowlist，fail-closed）。

    掃描每一行，取所有以 `:{port}` 結尾的 token，解出位址（`rsplit(":", 1)`
    取冒號前段，並去除 IPv6 的方括號）。只要找到至少一個 bind token，且全部
    位址皆屬於 loopback（`127.0.0.1`／`127.*`／`::1`），才回傳 True。

    任何非 loopback 位址（wildcard `0.0.0.0`/`::`/`*`/空字串、特定介面 IP 如
    `10.0.0.5`、global IPv6 等）→ 回傳 False。完全找不到 bind token → 回傳
    False（fail-closed，寧可誤判不安全也不誤判安全）。
    """
    found = False
    for line in ss_out.splitlines():
        if f":{port}" not in line:
            continue
        for tok in line.split():
            if tok.endswith(f":{port}"):
                addr = tok.rsplit(":", 1)[0].strip("[]")
                found = True
                if not (addr == "127.0.0.1" or addr.startswith("127.") or addr == "::1"):
                    return False
    return found


class _Clock(Protocol):
    """注入時鐘介面（結構化型別）：需提供 `monotonic()` 與 `sleep(seconds)`。

    預設不注入時直接使用標準函式庫 `time` 模組（其 `monotonic`/`sleep`
    天然滿足此介面）；測試可注入假時鐘加速 readiness 輪詢。
    """

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


def _reap_pgid(pgid: int) -> None:
    """reap 已結束的同群子行程，避免 zombie 讓 killpg(pgid,0) 誤判仍存活。

    僅在本行程為該子行程之父時有效（生產 CLI 與測試皆是）；非父行程時 os.waitpid
    會拋 ChildProcessError，靜默忽略。"""
    while True:
        try:
            reaped, _ = os.waitpid(-pgid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError, OSError):
            return
        if reaped == 0:
            return


def _terminate_pgid(
    pgid: int,
    *,
    timeout: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """SIGTERM 後有界等待行程群組消失，逾時仍存活才 SIGKILL（fail-closed 拆除）。

    用於 `open_tunnel` readiness 失敗時的收尾：確保不留下已 spawn 但未驗證
    安全性（例如遠端 bind 未經確認為 loopback）的隧道行程。`ProcessLookupError`
    （群組已消失）／`PermissionError`（非我方行程，理論上不會發生）皆吞掉，
    不讓拆除動作本身拋例外。

    輪詢迴圈每次疊代開頭先 `_reap_pgid()`：被 SIGTERM 殺死的子行程若不被
    父行程回收會留下 zombie，而 zombie 的 pgid slot 仍在，導致
    `os.killpg(pgid, 0)` 存活探測持續誤判「仍存活」直到跑滿整個
    `timeout` 才 fallback 到（對 zombie 而言是 no-op 的）SIGKILL。本行程
    通常即為該子行程之父（生產 CLI 的 `subprocess.Popen` 單 fork，以及本模組
    測試 fixture），故主動 reap 可讓 teardown 及時收斂，不必吃滿整個逾時。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    waited = 0.0
    while waited < timeout:
        _reap_pgid(pgid)
        try:
            os.killpg(pgid, 0)  # 存活探測：不送訊號，僅檢查群組是否還在
        except ProcessLookupError:
            return
        sleep(0.1)
        waited += 0.1
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    _reap_pgid(pgid)


def open_tunnel(
    spec: TunnelSpec,
    run_dir: str,
    *,
    spawner: Callable[[list[str], str, str], tuple[int, int, int | None, Callable[[], str]]],
    runner: Callable[[list[str]], tuple[int, str]],
    ping: Callable[[str], bool],
    ssh_check_factory: Callable[..., Callable[[], bool]] = make_ssh_check,
    role_probe_factory: Callable[..., Callable[[], bool]] = make_role_probe,
    clock: _Clock | None = None,
) -> dict:
    """編排單一隧道的完整開啟流程：spawn → durable state → readiness → active/starting。

    流程（`spawner(argv, control_path, log_path) -> (pid, pgid, start_ticks, stderr_tail_fn)`）：

    1. 持 `Registry.lock()`：identity 衝突檢查 → `spawner` 產生行程 →
       **立即寫入 `status="spawning"` 的 durable state** → 釋放鎖。
       （不在鎖內跑 readiness，避免長時間輪詢阻塞 `remote list`/`remote close`
       等其他需要鎖的操作。）
    2. 鎖外跑 `wait_ready`：成功回 `active`／`starting`，重新取鎖寫回 state。
    3. `wait_ready` raise `TunnelError`（含 `REMOTE_BIND_UNVERIFIED`、
       `TUNNEL_SPAWN_FAILED`）→ **fail-closed**：`_terminate_pgid()` 拆除已 spawn
       的行程群組，並移除 durable state（不留下未驗證安全性的暴露隧道），
       然後 re-raise。

    同 `listen_port` 已有存活且相同 identity 的隧道視為冪等（`already_running`
    no-op）；存活但不同 identity 視為衝突（raise `TUNNEL_CONFLICT`，避免竊佔
    他人隧道或造成埠位混用）；已死亡的殘留 state 視為過期，覆寫重來。
    """
    reg = Registry(run_dir)
    listen_port = spec.local if (spec.role == "connect" and spec.local is not None) else spec.port
    identity = compute_identity(spec)
    control_path = reg.control_path(listen_port)

    # ── identity 衝突檢查 → spawn → 立即寫 durable state（持鎖）──
    with reg.lock():
        existing = reg.read(listen_port)
        if existing:
            existing_pid = int(existing.get("pid", -1))
            # pid<=0 一律視為死亡：os.kill(-1, 0) 會 POSIX-broadcast 且無條件成功，
            # 若讓 pid_alive() 收到非正值 pid，缺 pid 欄位的殘缺 state 會被誤判「存活」。
            alive = existing_pid > 0 and pid_alive(existing_pid, existing.get("pid_start_ticks"))
            if alive and existing.get("identity") == identity:
                return {"ok": True, "already_running": True, **_public(existing)}
            if alive and existing.get("identity") != identity:
                raise TunnelError(
                    "TUNNEL_CONFLICT",
                    f"port {listen_port} 已有不同 identity 的隧道，請先 remote close {listen_port}",
                )
            reg.remove(listen_port)  # 死 state → 視為過期，覆寫

        log_path = os.path.join(reg.remote_dir, f"{listen_port}.log")
        pid, pgid, start_ticks, stderr_tail = spawner(build_argv(spec, control_path), control_path, log_path)
        state = {
            "identity": identity, "status": "spawning", "role": spec.role,
            "pid": pid, "pgid": pgid, "pid_start_ticks": start_ticks,
            "target": spec.ssh_target, "listen_port": listen_port,
            "remote_bind": spec.remote_socket or f"127.0.0.1:{spec.port}",
            "forward_target": spec.forward_src, "via": spec.via,
            "control_path": control_path, "endpoint": endpoint_for(spec),
        }
        reg.write(state)

    # ── readiness（不持鎖，避免阻塞其他操作）──
    active_clock: _Clock = clock or time  # type: ignore[assignment]
    monotonic = active_clock.monotonic
    sleep = active_clock.sleep
    endpoint = endpoint_for(spec)
    ssh_check = ssh_check_factory(spec, control_path, runner=runner)
    role_probe = role_probe_factory(spec, control_path, endpoint, runner=runner, ping=ping)

    try:
        status = wait_ready(
            spec, pid, start_ticks,
            ssh_check=ssh_check, role_probe=role_probe,
            sleep=sleep, monotonic=monotonic,
            alive=lambda: pid_alive(pid, start_ticks),
            stderr_tail=stderr_tail,
        )
    except BaseException:
        # fail-closed：teardown 對任何 readiness 例外都要觸發（不只 TunnelError；
        # 含 probe 逾時、Ctrl-C 等），拆除已 spawn 的行程群組並移除 state，
        # 不留下暴露隧道，再原樣 re-raise。
        _terminate_pgid(pgid, sleep=sleep)
        with reg.lock():
            reg.remove(listen_port)
        raise

    with reg.lock():
        state["status"] = status
        reg.write(state)
    return {"ok": True, **_public(state)}


def status(run_dir: str) -> dict:
    """列出所有隧道；就地 prune 已死的 state，並掃描孤兒 control socket。

    pid<=0（缺 pid 欄位的殘缺 state）一律視為死亡並 prune，不呼叫
    `pid_alive()`（同 `open_tunnel` 的 pid<=0 守衛慣例：`os.kill(-1, 0)` 會
    POSIX-broadcast 且無條件成功，會把殘缺 state 誤判為存活）。
    """
    reg = Registry(run_dir)
    tunnels: list[dict] = []
    with reg.lock():
        for st in reg.read_all():
            pid = int(st.get("pid", -1))
            if pid > 0 and pid_alive(pid, st.get("pid_start_ticks")):
                out = _public(st)
                out["alive"] = True
                tunnels.append(out)
            else:
                reg.remove(int(st["listen_port"]))  # prune 死 state
        # orphan scan：cm-* control socket 檔案存在卻無對應 state
        # （ssh master 仍活著，但 state 遺失／未寫入的孤兒）。
        try:
            for name in sorted(os.listdir(reg.remote_dir)):
                if name.startswith("cm-") and not reg.read(_port_of_cm(name)):
                    tunnels.append({
                        "status": "orphan",
                        "control_path": os.path.join(reg.remote_dir, name),
                        "alive": True,
                    })
        except OSError:
            pass
    return {"ok": True, "tunnels": tunnels}


def _port_of_cm(name: str) -> int:
    """從 `cm-<port>` 控制端點檔名解析 port；解析失敗回 -1（不會命中任何 state）。"""
    try:
        return int(name[len("cm-"):])
    except ValueError:
        return -1


def close(run_dir: str, selector: str | int, *, sleep: Callable[[float], None] = time.sleep) -> dict:
    """依 `selector`（port 或 `"all"`）關閉隧道：驗活 → killpg 整組行程 → wait → remove。

    找不到對應 port（已被 prune 或本就不存在）視為冪等 no-op，回
    `{"ok": True, "closed": []}`，不視為錯誤。`_terminate_pgid` 拆除失敗時
    保留 `status="error"` 的 state（不 remove），供後續人工排查；pid<=0
    的殘缺 state 直接跳過 killpg、視為已死並 remove（同 `status` 的守衛慣例）。
    """
    reg = Registry(run_dir)
    closed: list[int] = []
    with reg.lock():
        targets = reg.read_all()
        if str(selector) != "all":
            targets = [s for s in targets if str(s.get("listen_port")) == str(selector)]
        for st in targets:
            port = int(st["listen_port"])
            pid = int(st.get("pid", -1))
            pgid = int(st.get("pgid", pid))
            if pid > 0 and pid_alive(pid, st.get("pid_start_ticks")):
                try:
                    _terminate_pgid(pgid, sleep=sleep)
                except Exception:  # noqa: BLE001 — 拆除失敗不讓例外穿越 RPC/CLI 邊界，改記錄 error 狀態
                    st["status"] = "error"
                    reg.write(st)
                    continue
            reg.remove(port)
            closed.append(port)
    return {"ok": True, "closed": closed}


def _public(state: dict) -> dict:
    """對外可見欄位（去掉 control_path 等內部細節）。"""
    keys = ("status", "role", "pid", "listen_port", "target", "remote_bind",
            "forward_target", "via", "endpoint", "identity")
    out = {k: state[k] for k in keys if k in state}
    if state.get("role") == "expose":
        out["remote_hint"] = (
            f"agent 端用 serialwrap --endpoint "
            f"{state.get('endpoint', 'tcp://127.0.0.1:%d' % state['listen_port'])}"
        )
    return out


def guard_platform() -> None:
    """本期 remote 隧道僅支援 POSIX；native Windows 明確拒絕（改走手動 ssh -R）。"""
    if os.name == "nt":
        raise TunnelError(
            "REMOTE_NOT_SUPPORTED",
            "native Windows 本期不支援 serialwrap remote；請手動 ssh -R（見 SKILL_WINDOWS.md）",
        )


def resolve_ssh_bin(via: str) -> str:
    """以 `shutil.which` 探索 ssh／autossh 執行檔路徑；PATH 找不到即 fail-closed。"""
    name = "autossh" if via == "autossh" else "ssh"
    path = shutil.which(name)
    if not path:
        raise TunnelError("SSH_NOT_FOUND", f"PATH 找不到 {name}")
    return path


def real_spawner(
    argv: list[str], control_path: str, log_path: str
) -> tuple[int, int, int | None, Callable[[], str]]:
    """以 `subprocess.Popen` 背景常駐產生 ssh/autossh 行程，供 `open_tunnel` 的 `spawner` 參數注入。

    `start_new_session=True` 讓子行程獨立成一個新 process group（獨立 pgid），
    供之後 `_terminate_pgid()` 以 `killpg` 整組拆除（含 ssh 本身衍生的子行程）；
    stdout/stderr 皆導向 `log_path`，供 readiness 失敗時的 `stderr_tail_fn` 讀取
    診斷訊息。父行程端的 log handle 在 `Popen` 後即關閉——子行程已透過
    `stdout=`/`stderr=` 取得自己 dup 過的 fd，父行程端保留原 handle 只會洩漏 fd。
    """
    os.makedirs(os.path.dirname(control_path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()  # 子行程已 dup 走自己的 fd；父行程端不再需要，避免 fd 洩漏
    pgid = os.getpgid(proc.pid)
    start_ticks = read_pid_start_ticks(proc.pid)

    def _stderr_tail() -> str:
        """讀取 spawn log 尾端（供 `TUNNEL_SPAWN_FAILED` 附診斷訊息）；每次呼叫以路徑重新開檔，
        不依賴上方已關閉的 `log` handle。"""
        try:
            with open(log_path, "rb") as fh:
                return fh.read()[-500:].decode("utf-8", "replace")
        except OSError:
            return ""

    return proc.pid, pgid, start_ticks, _stderr_tail


def make_runner() -> Callable[[list[str]], tuple[int, str]]:
    """回傳 real `subprocess.run` runner，供 `make_ssh_check`／`make_role_probe` 的 `runner` 參數注入。"""
    def _run(argv: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=10.0, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # probe 逾時視為「未就緒」而非崩潰：保持在 readiness 狀態機內
            # （回傳非 0 rc → role_probe/ssh_check 判 False → wait_ready 續 poll
            # 或逾時回 "starting"），不讓 TimeoutExpired 穿越到 open_tunnel。
            partial = exc.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return 124, f"probe timeout: {partial}"[:500]
        return proc.returncode, proc.stdout or ""
    return _run


def real_ping(endpoint: str) -> bool:
    """以既有 RPC client 呼叫 `health.ping` 探測 endpoint 是否就緒；任何例外一律視為未就緒。"""
    try:
        return bool(_rpc_call(endpoint, "health.ping", {}, timeout_s=2.0).get("ok"))
    except Exception:  # noqa: BLE001 — probe 失敗不讓例外穿越，僅視為未就緒
        return False
