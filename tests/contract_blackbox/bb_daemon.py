"""以 daemon_prefix() 啟動的 Python daemon 包裝。

提供 subprocess 啟動、socket 與 health.ping 探測、daemon.stop 與清理，
並支援 $SW_AB_PIDLOG 記錄。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from bb_harness import daemon_prefix


def _send_rpc(endpoint: str, method: str, params: dict[str, Any], timeout_s: float = 1.0) -> dict[str, Any] | None:
    sock = None
    try:
        if "://" in endpoint:
            if endpoint.startswith("tcp://"):
                host_port = endpoint[6:]
                host, port_str = host_port.split(":", 1)
                sock = socket.create_connection((host, int(port_str)), timeout=timeout_s)
            elif endpoint.startswith("unix://"):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(timeout_s)
                sock.connect(endpoint[7:])
            else:
                return None
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(endpoint)

        payload = json.dumps({"id": 1, "method": method, "params": params}).encode("utf-8") + b"\n"
        sock.sendall(payload)

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return None
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            return None
        res = json.loads(line.decode("utf-8", errors="replace"))
        return res if isinstance(res, dict) else None
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _collect_pids(pid: int) -> list[int]:
    pids = [pid]
    queue = [pid]
    while queue:
        curr = queue.pop(0)
        task_dir = Path(f"/proc/{curr}/task")
        if task_dir.is_dir():
            try:
                for t in task_dir.iterdir():
                    child_file = t / "children"
                    if child_file.is_file():
                        try:
                            content = child_file.read_text().strip()
                            for c in content.split():
                                c_int = int(c)
                                if c_int not in pids:
                                    pids.append(c_int)
                                    queue.append(c_int)
                        except OSError:
                            pass
            except OSError:
                pass
    return pids


def _current_pid_ns() -> str | None:
    """本行程所屬 pid namespace 的連結字串（`pid:[inode]`）；非 POSIX 或無法讀取回 None。

    釋放檢查（scripts/rust/ab_blackbox_1b.py）以此區分「與 host 同 namespace」與「pidns_rig 子行程的內部 pid」：
    後者的 pid 數字是 namespace 內編號，不得拿去查 host 的 /proc/<pid>。
    """
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _proc_diag(pid: int) -> list[dict[str, str]]:
    """行程各 thread 的排程狀態與 wchan（`/proc/<pid>/task/*`）；供 stop 失敗時歸因（S1X Y3）。

    狀態 `D`（不可中斷睡眠）表示卡在核心 I/O，SIGKILL 也要等該 I/O 完成；`Z` 為已死待回收。讀取失敗回空清單。
    """
    rows: list[dict[str, str]] = []
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tasks = sorted(task_dir.iterdir(), key=lambda t: int(t.name))
    except (OSError, ValueError):
        return rows
    for task in tasks:
        row = {"tid": task.name}
        try:
            stat = (task / "stat").read_text()
            # comm 以括號包住且可能含空白：狀態欄位在最後一個 ')' 之後
            row["comm"] = stat[stat.index("(") + 1: stat.rindex(")")]
            row["state"] = stat[stat.rindex(")") + 2:].split(" ", 1)[0]
        except (OSError, ValueError):
            row["state"] = "?"
        for name in ("wchan", "syscall"):
            try:
                row[name] = (task / name).read_text().strip()
            except OSError:
                pass
        rows.append(row)
    return rows


def _stoplog(record: dict[str, Any]) -> None:
    """`$SW_AB_STOPLOG` 有設定時，以 NDJSON 附加一筆 stop 紀錄（各階段耗時與結果）。"""
    path = os.environ.get("SW_AB_STOPLOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


class PyDaemon:
    def __init__(self, env: Mapping[str, str], *, profile_dir: Path, socket: str, lock: Path) -> None:
        self.env = dict(env)
        self.profile_dir = Path(profile_dir)
        self.socket = socket
        self.lock = Path(lock)
        self.workdir = self.lock.parent
        self.proc: subprocess.Popen | None = None
        self._pids: list[int] = []

    def start(self, timeout_s: float = 15.0) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        stderr_path = self.workdir / "daemon.stderr"
        with open(stderr_path, "ab") as stderr_file:
            cmd = daemon_prefix() + [
                "--profile-dir",
                str(self.profile_dir),
                "--socket",
                self.socket,
                "--lock",
                str(self.lock),
            ]
            self.proc = subprocess.Popen(
                cmd,
                env=self.env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                start_new_session=(os.name == "posix"),
            )

        # 立即記錄 pid 到 pidlog（即使未達 ready 亦可供釋放檢查追蹤）
        pidlog = os.environ.get("SW_AB_PIDLOG")
        if pidlog:
            record = {
                "pid": self.proc.pid,
                "pid_ns": _current_pid_ns(),
                "children": [],
                "socket": self.socket,
                "lock": str(self.lock),
                "run_dir": str(self.workdir),
            }
            with open(pidlog, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")

        deadline = time.monotonic() + timeout_s
        ready = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Daemon process died prematurely with rc={self.proc.returncode}")
            res = _send_rpc(self.socket, "health.ping", {}, timeout_s=0.5)
            if res and res.get("ok"):
                ready = True
                break
            time.sleep(0.1)

        if not ready:
            self.stop()
            raise TimeoutError(f"Daemon failed to respond to health.ping within {timeout_s}s")

        self._pids = _collect_pids(self.proc.pid)
        if pidlog and len(self._pids) > 1:
            record = {
                "pid": self.proc.pid,
                "pid_ns": _current_pid_ns(),
                "children": [p for p in self._pids if p != self.proc.pid],
                "socket": self.socket,
                "lock": str(self.lock),
                "run_dir": str(self.workdir),
            }
            with open(pidlog, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def stop(self, timeout_s: float = 5.0) -> None:
        if self.proc is None:
            return
        proc = self.proc
        pid = proc.pid
        t0 = time.monotonic()
        record: dict[str, Any] = {"pid": pid, "socket": self.socket, "escalation": "rpc"}
        try:
            rpc = _send_rpc(self.socket, "daemon.stop", {}, timeout_s=1.0)
        except Exception:
            rpc = None
        record["rpc_ok"] = bool(rpc and rpc.get("ok"))
        record["rpc_s"] = round(time.monotonic() - t0, 3)

        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            record["escalation"] = "sigterm"
            record["diag_before_sigterm"] = _proc_diag(pid)
            if os.name == "posix":
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    pass
            else:
                proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                record["escalation"] = "sigkill"
                record["diag_before_sigkill"] = _proc_diag(pid)
                if os.name == "posix":
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

        record["exit_s"] = round(time.monotonic() - t0, 3)
        record["returncode"] = proc.poll()
        # 有界確認主行程消失
        if proc.poll() is None:
            diag = _proc_diag(pid)
            record["diag_after_sigkill"] = diag
            record["alive"] = True
            _stoplog(record)
            raise RuntimeError(
                f"Daemon process pid={pid} 於 stop 後仍存活（{record['exit_s']}s、升級至 {record['escalation']}；"
                f"threads={json.dumps(diag, ensure_ascii=False)}）"
            )
        _stoplog(record)

        # POSIX 下確認子行程消失
        if os.name == "posix":
            for child_pid in self._pids:
                if child_pid != pid:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except OSError:
                        pass
            # 輪詢等待子行程消失（最長 2s）
            deadline = time.monotonic() + 2.0
            alive_children = []
            while time.monotonic() < deadline:
                alive_children = []
                for child_pid in self._pids:
                    if child_pid != pid:
                        try:
                            os.kill(child_pid, 0)
                            alive_children.append(child_pid)
                        except OSError:
                            pass
                if not alive_children:
                    break
                time.sleep(0.05)
            if alive_children:
                for child_pid in alive_children:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except OSError:
                        pass
                time.sleep(0.1)
                final_alive = [c for c in alive_children if os.path.exists(f"/proc/{c}")]
                if final_alive:
                    raise RuntimeError(f"Daemon 子行程 {final_alive} 於 stop 後仍未消失")

        # 確認 socket 消失
        sock_path: Path | None = None
        if self.socket.startswith("unix://"):
            sock_path = Path(self.socket[7:])
        elif "://" not in self.socket:
            sock_path = Path(self.socket)

        if sock_path:
            sock_deadline = time.monotonic() + 2.0
            while sock_path.exists() and time.monotonic() < sock_deadline:
                time.sleep(0.05)
            if sock_path.exists():
                try:
                    sock_path.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"Daemon socket {sock_path} 於 stop 後仍未消失")

        self.proc = None

    @property
    def pids(self) -> list[int]:
        return list(self._pids)
