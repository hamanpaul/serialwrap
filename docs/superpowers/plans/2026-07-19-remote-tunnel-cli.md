# serialwrap `remote` 隧道便利 CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `serialwrap remote` 子命令群，用極簡 `host:port` 語法在 runtime 按需拉起／關閉 SSH 反向隧道，讓遠端 agent 取得本機 serialwrap 操作，serialwrapd 零改動、不重啟、不做預設。

**Architecture:** 純 CLI 便利層——新模組 `sw_core/remote_tunnel.py` 外包給系統 `ssh`（`-R` expose／`-L` connect），background 常駐 + flock registry + readiness 確認 + robust close；`sw_core/cli.py` 加平面 `remote` 分派。daemon／RPC／arbiter 完全不動。

**Tech Stack:** Python 3.10+、stdlib（`subprocess`／`fcntl`／`socket`／`hashlib`／`argparse`）、系統 `ssh`/`autossh`；測試 pytest + docker（真 sshd）。

**Spec:** `docs/superpowers/specs/2026-07-19-remote-tunnel-cli-design.md`（含 2 輪 adversarial review 修訂）。

## Global Constraints

- **daemon 零改動**：不改 `sw_core/service.py`／`daemon.py`／`arbiter.py`／`session_manager.py`／RPC；serialwrapd 不重啟、不新增 listener。
- **不做預設**：`remote` 永不自動啟動，只在顯式執行時開。
- **僅 POSIX（Linux/WSL）**：native Windows（`os.name == "nt"`）執行 `remote` → 回 `REMOTE_NOT_SUPPORTED`；lifecycle 用 POSIX primitive（`start_new_session`／`fcntl.flock`／`os.killpg`／`/proc` start-ticks）。
- **auth 全交 ssh**：不碰金鑰、不引入 paramiko；預設 `-o BatchMode=yes`。
- **loopback bind 不變量**：`-L`／`-R` 顯式綁 `127.0.0.1:`；`-R` tcp 模式 active 前必以 `ss` 實測遠端 bind，非 loopback／無法驗證即拆除 + `REMOTE_BIND_UNVERIFIED`。
- **JSON 輸出**：一律經 `sw_core/cli.py::_print`（`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",",":"))`）；失敗回 `{"ok":false,"error_code":...}`，例外不穿越。
- **語言**：所有 code comment／docstring／文件繁體中文（README 中英雙語並存）。
- **測試政策**：`python3 -m pytest -q tests/` 無新失敗；`python3 -m policy_check --repo .` 通過。既有已知失敗 `tests/test_multiagent_e2e.py::...::test_five_agents_three_rounds_no_conflict` 不計。
- **分支**：`feature/remote-tunnel-cli`（已建立，禁止 commit 到 main）。
- **每個 code 變更前**：新增／更新 `changelog.d/remote-tunnel-cli.md`（本計畫 Task 11 一次備妥）。
- **commit trailer**：每個 commit 附 `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`。

---

### Task 1: 模組骨架 + target 解析 + identity

**Files:**
- Create: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Produces:
  - `class TunnelError(Exception)`：`__init__(self, code: str, message: str | None = None)`，屬性 `.code`、`.message`。
  - `@dataclass(frozen=True) class TunnelSpec`：欄位 `role: str`（`"expose"|"connect"`）、`ssh_target: str`、`port: int`、`local: int | None = None`、`forward_src: str | None = None`、`remote_socket: str | None = None`、`via: str = "ssh"`、`ssh_opts: tuple[str, ...] = ()`、`ready_timeout: float = 10.0`。
  - `def parse_target(target: str) -> tuple[str, int]`：回 `(ssh_target, port)`；不符 `[user@]host:port` → `raise TunnelError("INVALID_TARGET", ...)`。
  - `def compute_identity(spec: TunnelSpec) -> str`：canonical effective forward spec 的 `sha256` hex。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py
from __future__ import annotations

import pytest

from sw_core import remote_tunnel as rt


def test_parse_target_user_host_port():
    assert rt.parse_target("tester@relay:7777") == ("tester@relay", 7777)


def test_parse_target_alias_only():
    assert rt.parse_target("myrelay:22000") == ("myrelay", 22000)


@pytest.mark.parametrize("bad", ["nohost", "host:", "host:abc", "host:-1", "", "a@b@c:1"])
def test_parse_target_rejects_bad(bad):
    with pytest.raises(rt.TunnelError) as ei:
        rt.parse_target(bad)
    assert ei.value.code == "INVALID_TARGET"


def test_identity_stable_and_distinguishes_remote_socket():
    a = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    b = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    c = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock",
                      remote_socket="/relay/b.sock")
    assert rt.compute_identity(a) == rt.compute_identity(b)
    assert rt.compute_identity(a) != rt.compute_identity(c)


def test_identity_distinguishes_via_and_ssh_opts():
    base = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    assert rt.compute_identity(base) != rt.compute_identity(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock", via="autossh"))
    assert rt.compute_identity(base) != rt.compute_identity(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock",
                      ssh_opts=("-p", "2222")))
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -q`
Expected: FAIL（`ModuleNotFoundError` 或 `AttributeError: parse_target`）。

- [ ] **Step 3: 寫最小實作**

```python
# sw_core/remote_tunnel.py
"""serialwrap `remote` 隧道便利層（純 CLI，daemon 零改動）。

外包給系統 ssh 建立 `-R`（expose）／`-L`（connect）隧道，background 常駐、
flock registry 管理、readiness 確認。設計見
docs/superpowers/specs/2026-07-19-remote-tunnel-cli-design.md。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

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
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -q`
Expected: PASS（5 passed）。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): remote_tunnel 模組骨架與 target 解析／identity" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: `build_argv`（ssh/autossh argv 組裝）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: `TunnelSpec`（Task 1）。
- Produces:
  - `def default_ssh_opts(control_path: str) -> list[str]`：回預設 `-o` 清單。
  - `def build_argv(spec: TunnelSpec, control_path: str) -> list[str]`：完整 `ssh`/`autossh` argv（含方向 `-R`/`-L`、預設 `-o`、`--ssh-opt` 透傳、target）。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
def _cp():
    return "/run/user/1000/serialwrap/remote/cm-abc"


def test_build_argv_expose_unix_socket_loopback_bind():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/run/serialwrapd.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[0] == "ssh"
    assert "-N" in argv and "-T" in argv
    assert "-R" in argv
    assert argv[argv.index("-R") + 1] == "127.0.0.1:7777:/run/serialwrapd.sock"
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "ExitOnForwardFailure=yes" in joined
    assert "ControlMaster=auto" in joined
    assert f"ControlPath={_cp()}" in joined
    assert argv[-1] == "u@h"


def test_build_argv_expose_remote_socket_hardened():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/run/serialwrapd.sock", remote_socket="/relay/x.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-R") + 1] == "/relay/x.sock:/run/serialwrapd.sock"


def test_build_argv_expose_tcp_forward_src():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="127.0.0.1:48700")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-R") + 1] == "127.0.0.1:7777:127.0.0.1:48700"


def test_build_argv_connect_tcp_loopback():
    spec = rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7777)
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-L") + 1] == "127.0.0.1:7777:localhost:7777"


def test_build_argv_connect_remote_socket():
    spec = rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7788,
                         remote_socket="/relay/x.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-L") + 1] == "127.0.0.1:7788:/relay/x.sock"


def test_build_argv_autossh_and_ssh_opts_passthrough():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/s.sock", via="autossh", ssh_opts=("-p", "2222"))
    argv = rt.build_argv(spec, _cp())
    assert argv[0] == "autossh"
    assert argv[1:3] == ["-M", "0"]
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -q`
Expected: FAIL（`AttributeError: build_argv`）。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
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
    base = ["autossh", "-M", "0"] if spec.via == "autossh" else ["ssh"]
    argv = [*base, "-N", "-T", *default_ssh_opts(control_path), *spec.ssh_opts]
    argv += _direction_args(spec)
    argv.append(spec.ssh_target)
    return argv
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): build_argv 組裝 ssh/autossh -R/-L 與安全預設 -o" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: state registry（flock + durable state + pid_start_ticks 存活 + prune）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: `TunnelSpec`、`compute_identity`（Tasks 1）。
- Produces:
  - `def read_pid_start_ticks(pid: int) -> int | None`：讀 `/proc/<pid>/stat` 第 22 欄；讀不到回 `None`。
  - `def pid_alive(pid: int, start_ticks: int | None) -> bool`：`os.kill(pid,0)` + start-ticks 一致性（防 PID reuse）。
  - `class Registry`：`__init__(self, run_dir: str)`；`remote_dir`（`<run_dir>/remote`）、`lock()`（contextmanager，flock LOCK_EX）、`control_path(listen_port: int) -> str`、`state_path(listen_port: int) -> str`、`write(state: dict) -> None`（atomic `os.replace`）、`read(listen_port: int) -> dict | None`、`read_all() -> list[dict]`、`remove(listen_port: int) -> None`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
import json
import os
import subprocess


def test_registry_write_read_remove_atomic(tmp_path):
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose"})
    assert reg.read(7777)["status"] == "active"
    assert [s["listen_port"] for s in reg.read_all()] == [7777]
    with reg.lock():
        reg.remove(7777)
    assert reg.read(7777) is None


def test_pid_alive_true_for_running_and_reuse_guard(tmp_path):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        ticks = rt.read_pid_start_ticks(proc.pid)
        assert ticks is not None
        assert rt.pid_alive(proc.pid, ticks) is True
        # start-ticks 不符 → 視為非我方（PID reuse 防護）
        assert rt.pid_alive(proc.pid, ticks + 999999) is False
    finally:
        proc.terminate()
        proc.wait()


def test_pid_alive_false_for_dead(tmp_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert rt.pid_alive(proc.pid, 12345) is False


def test_lock_is_exclusive_serialized(tmp_path):
    # 兩次 lock 不會同時持有（LOCK_EX）；此處驗證可重入取得、釋放後再取。
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        pass
    with reg.lock():
        reg.write({"listen_port": 1, "status": "spawning", "role": "connect"})
    assert reg.read(1)["status"] == "spawning"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "registry or pid_alive or lock" -q`
Expected: FAIL（`AttributeError: Registry`）。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
import contextlib
import fcntl
import json
import os


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


class Registry:
    """`<run_dir>/remote/` 下的 per-tunnel state JSON + flock 序列化。"""

    def __init__(self, run_dir: str) -> None:
        self.run_dir = run_dir
        self.remote_dir = os.path.join(run_dir, "remote")

    def _ensure_dir(self) -> None:
        os.makedirs(self.remote_dir, exist_ok=True)

    @contextlib.contextmanager
    def lock(self):
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
        return os.path.join(self.remote_dir, f"cm-{listen_port}")

    def state_path(self, listen_port: int) -> str:
        return os.path.join(self.remote_dir, f"{listen_port}.json")

    def write(self, state: dict) -> None:
        self._ensure_dir()
        path = self.state_path(int(state["listen_port"]))
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, path)  # atomic

    def read(self, listen_port: int) -> dict | None:
        try:
            with open(self.state_path(listen_port), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def read_all(self) -> list[dict]:
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
        for p in (self.state_path(listen_port), self.control_path(listen_port)):
            with contextlib.suppress(OSError):
                os.unlink(p)
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "registry or pid_alive or lock" -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): flock registry + durable state + pid_start_ticks 存活驗證" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: readiness（注入式 probe：ssh -O check / ss 遠端 bind / health.ping）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: `TunnelSpec`（Task 1）。
- Produces:
  - `def wait_ready(spec, pid, start_ticks, *, ssh_check, role_probe, sleep, monotonic) -> str`：回 `"active"` | `"starting"`；行程死亡 → `raise TunnelError("TUNNEL_SPAWN_FAILED", <stderr尾>)`。`ssh_check() -> bool`（master 是否已連線＋forward 建立）、`role_probe() -> bool`（`-R`：遠端 bind loopback 驗證；`-L`：`health.ping`）皆為注入 callable；`sleep`／`monotonic` 注入時鐘。遠端 bind 驗到 wildcard 時 `role_probe` 內部 `raise TunnelError("REMOTE_BIND_UNVERIFIED")`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
class _Clock:
    def __init__(self):
        self.t = 0.0
    def monotonic(self):
        return self.t
    def sleep(self, dt):
        self.t += dt


def _spec_R():
    return rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock",
                         ready_timeout=5.0)


def test_wait_ready_active_when_check_and_probe_ok():
    clk = _Clock()
    st = rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                       ssh_check=lambda: True, role_probe=lambda: True,
                       sleep=clk.sleep, monotonic=clk.monotonic,
                       alive=lambda: True, stderr_tail=lambda: "")
    assert st == "active"


def test_wait_ready_starting_on_timeout_process_alive():
    clk = _Clock()
    st = rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                       ssh_check=lambda: False, role_probe=lambda: False,
                       sleep=clk.sleep, monotonic=clk.monotonic,
                       alive=lambda: True, stderr_tail=lambda: "")
    assert st == "starting"


def test_wait_ready_spawn_failed_when_process_dies():
    clk = _Clock()
    with pytest.raises(rt.TunnelError) as ei:
        rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                      ssh_check=lambda: False, role_probe=lambda: False,
                      sleep=clk.sleep, monotonic=clk.monotonic,
                      alive=lambda: False, stderr_tail=lambda: "Permission denied (publickey).")
    assert ei.value.code == "TUNNEL_SPAWN_FAILED"
    assert "publickey" in (ei.value.message or "")


def test_wait_ready_remote_bind_unverified_propagates():
    clk = _Clock()
    def probe():
        raise rt.TunnelError("REMOTE_BIND_UNVERIFIED", "0.0.0.0:7777")
    with pytest.raises(rt.TunnelError) as ei:
        rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                      ssh_check=lambda: True, role_probe=probe,
                      sleep=clk.sleep, monotonic=clk.monotonic,
                      alive=lambda: True, stderr_tail=lambda: "")
    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k wait_ready -q`
Expected: FAIL（`AttributeError: wait_ready`）。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
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
    """回 'active'（就緒）或 'starting'（逾時但行程仍活）；行程死亡 → TUNNEL_SPAWN_FAILED。"""
    deadline = monotonic() + spec.ready_timeout
    while True:
        if not alive():
            raise TunnelError("TUNNEL_SPAWN_FAILED", (stderr_tail() or "").strip()[-500:])
        if ssh_check() and role_probe():   # role_probe 可 raise（REMOTE_BIND_UNVERIFIED）
            return "active"
        if monotonic() >= deadline:
            return "starting"
        sleep(_POLL_INTERVAL_S)
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k wait_ready -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): wait_ready readiness 狀態機（注入式 probe，active/starting/spawn-failed）" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: probe 具體實作（ssh -O check / ss 遠端 bind / health.ping）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: `TunnelSpec`、`build_argv` 的 control_path 慣例。
- Produces（皆接受注入 `runner`／`ping` 以利測試）：
  - `def make_ssh_check(spec, control_path, *, runner) -> callable`：回 `() -> bool`，內部跑 `ssh -O check -o ControlPath=... <target>`（`runner(argv) -> (rc, stderr)`）。
  - `def make_role_probe(spec, control_path, endpoint, *, runner, ping) -> callable`：`-R` tcp → 在 relay 跑 `ss` 驗遠端 bind（wildcard → `raise TunnelError("REMOTE_BIND_UNVERIFIED", bind)`）；`-R` unix（`remote_socket`）→ 直接回 `True`；`-L` → `ping(endpoint) -> bool`。
  - `def endpoint_for(spec) -> str`：`-L`／direct 回 `tcp://127.0.0.1:<local>`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
def test_ssh_check_true_on_rc0():
    calls = []
    def runner(argv):
        calls.append(argv)
        return (0, "")
    chk = rt.make_ssh_check(rt.TunnelSpec(role="expose", ssh_target="u@h", port=1),
                            "/cp", runner=runner)
    assert chk() is True
    assert "-O" in calls[0] and "check" in calls[0]


def test_role_probe_expose_tcp_rejects_wildcard():
    def runner(argv):
        return (0, "LISTEN 0 128 0.0.0.0:7777 0.0.0.0:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    with pytest.raises(rt.TunnelError) as ei:
        probe()
    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"


def test_role_probe_expose_tcp_accepts_loopback():
    def runner(argv):
        return (0, "LISTEN 0 128 127.0.0.1:7777 0.0.0.0:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    assert probe() is True


def test_role_probe_expose_remote_socket_skips_bind_check():
    def runner(argv):
        raise AssertionError("unix 模式不應跑 ss")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock",
                      remote_socket="/relay/x.sock"),
        "/cp", "", runner=runner, ping=lambda ep: True)
    assert probe() is True


def test_role_probe_connect_uses_health_ping():
    seen = {}
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7777),
        "/cp", "tcp://127.0.0.1:7777",
        runner=lambda argv: (0, ""), ping=lambda ep: seen.setdefault("ep", ep) or True)
    assert probe() is True
    assert seen["ep"] == "tcp://127.0.0.1:7777"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "ssh_check or role_probe" -q`
Expected: FAIL。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
import re as _re

_WILDCARD_RE = _re.compile(r"(?:^|\s)(0\.0\.0\.0|\[?::\]?|\*):%d\b")


def endpoint_for(spec: TunnelSpec) -> str:
    local = spec.local if spec.local is not None else spec.port
    return f"tcp://127.0.0.1:{local}"


def make_ssh_check(spec: TunnelSpec, control_path: str, *, runner):
    def _check() -> bool:
        rc, _ = runner(["ssh", "-O", "check", "-o", f"ControlPath={control_path}", spec.ssh_target])
        return rc == 0
    return _check


def make_role_probe(spec: TunnelSpec, control_path: str, endpoint: str, *, runner, ping):
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
        if _WILDCARD_RE.search(out.replace("%d", str(spec.port))) or _bind_has_wildcard(out, spec.port):
            raise TunnelError("REMOTE_BIND_UNVERIFIED", out.strip()[:200])
        return True
    return _probe


def _bind_has_wildcard(ss_out: str, port: int) -> bool:
    """ss 輸出中該 port 是否綁在非 loopback 位址（0.0.0.0 / :: / *）。"""
    for line in ss_out.splitlines():
        if f":{port}" not in line:
            continue
        for tok in line.split():
            if tok.endswith(f":{port}"):
                addr = tok.rsplit(":", 1)[0].strip("[]")
                if addr in ("0.0.0.0", "::", "*", ""):
                    return True
    return False
```

> 註：`_WILDCARD_RE` 為保險；主判定走 `_bind_has_wildcard`（逐 token 比對位址）。實作者可擇一，測試以 `_bind_has_wildcard` 行為為準。

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "ssh_check or role_probe" -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): probe 實作（ssh -O check、ss 遠端 bind fail-closed、health.ping）" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: `open_tunnel` 編排（spawn → durable state → readiness → active/starting，conflict + fail-closed teardown）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: 全部 Task 1-5。
- Produces:
  - `def open_tunnel(spec, run_dir, *, spawner, ssh_check_factory=make_ssh_check, role_probe_factory=make_role_probe, runner=..., ping=..., clock=...) -> dict`。`spawner(argv, control_path, log_path) -> (pid, pgid, start_ticks, stderr_tail_fn)`。回 `{"ok":true,"status":...,"role":...,"pid":...,"listen_port":...,...}` 或 `raise TunnelError`。
  - 流程（持 `Registry.lock()`）：identity 衝突檢查 → spawn → **立即寫 `status="spawning"` durable state** → 釋放鎖跑 readiness → 依結果 `os.replace` 成 `active`/`starting`；readiness raise（含 `REMOTE_BIND_UNVERIFIED`/`TUNNEL_SPAWN_FAILED`）→ **拆除隧道 + remove state** 後 re-raise。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
class _FakeProc:
    def __init__(self, pid): self.pid = pid
    def poll(self): return None


def _fake_spawner(alive=True, stderr=""):
    procs = {}
    def spawn(argv, control_path, log_path):
        proc = subprocess.Popen(["sleep", "30"]) if alive else subprocess.Popen(["true"])
        if not alive:
            proc.wait()
        procs["p"] = proc
        return proc.pid, os.getpgid(proc.pid) if alive else proc.pid, \
               rt.read_pid_start_ticks(proc.pid), (lambda: stderr)
    spawn.procs = procs
    return spawn


def test_open_tunnel_expose_active(tmp_path):
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    spawn = _fake_spawner(alive=True)
    try:
        res = rt.open_tunnel(spec, str(tmp_path), spawner=spawn,
                             runner=lambda a: (0, "LISTEN 0 128 127.0.0.1:7777 *:*"),
                             ping=lambda ep: True)
        assert res["ok"] and res["status"] == "active" and res["role"] == "expose"
        reg = rt.Registry(str(tmp_path))
        assert reg.read(7777)["status"] == "active"
    finally:
        spawn.procs["p"].terminate(); spawn.procs["p"].wait()


def test_open_tunnel_conflict_same_port_diff_identity(tmp_path):
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                   "identity": "OTHER", "pid": os.getpid(),
                   "pid_start_ticks": rt.read_pid_start_ticks(os.getpid())})
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    with pytest.raises(rt.TunnelError) as ei:
        rt.open_tunnel(spec, str(tmp_path), spawner=_fake_spawner(),
                       runner=lambda a: (0, ""), ping=lambda ep: True)
    assert ei.value.code == "TUNNEL_CONFLICT"


def test_open_tunnel_fail_closed_removes_state_on_bind_unverified(tmp_path):
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    spawn = _fake_spawner(alive=True)
    try:
        with pytest.raises(rt.TunnelError) as ei:
            rt.open_tunnel(spec, str(tmp_path), spawner=spawn,
                           runner=lambda a: (0, "LISTEN 0 128 0.0.0.0:7777 *:*"),  # wildcard
                           ping=lambda ep: True)
        assert ei.value.code == "REMOTE_BIND_UNVERIFIED"
        assert rt.Registry(str(tmp_path)).read(7777) is None  # 拆除、不留暴露隧道
    finally:
        with contextlib.suppress(Exception):
            spawn.procs["p"].wait(timeout=2)
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k open_tunnel -q`
Expected: FAIL。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
import signal
import time


def _terminate_pgid(pgid: int, *, timeout: float = 5.0, sleep=time.sleep) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    waited = 0.0
    while waited < timeout:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        sleep(0.1)
        waited += 0.1
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def open_tunnel(
    spec: TunnelSpec,
    run_dir: str,
    *,
    spawner,
    runner,
    ping,
    ssh_check_factory=make_ssh_check,
    role_probe_factory=make_role_probe,
    clock=None,
) -> dict:
    reg = Registry(run_dir)
    listen_port = spec.local if (spec.role == "connect" and spec.local is not None) else spec.port
    identity = compute_identity(spec)
    control_path = reg.control_path(listen_port)

    # ── check → spawn → durable state（持鎖）──
    with reg.lock():
        existing = reg.read(listen_port)
        if existing:
            alive = pid_alive(int(existing.get("pid", -1)), existing.get("pid_start_ticks"))
            if alive and existing.get("identity") == identity:
                return {"ok": True, "already_running": True, **_public(existing)}
            if alive and existing.get("identity") != identity:
                raise TunnelError("TUNNEL_CONFLICT",
                                  f"port {listen_port} 已有不同 identity 的隧道，請先 remote close {listen_port}")
            reg.remove(listen_port)  # 死 state → 覆寫
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
    import time as _t
    monotonic = (clock or _t).monotonic
    sleep = (clock or _t).sleep
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
    except TunnelError:
        _terminate_pgid(pgid, sleep=sleep)
        with reg.lock():
            reg.remove(listen_port)
        raise

    with reg.lock():
        state["status"] = status
        reg.write(state)
    return {"ok": True, **_public(state)}


def _public(state: dict) -> dict:
    """對外可見欄位（去掉 argv 等內部細節）。"""
    keys = ("status", "role", "pid", "listen_port", "target", "remote_bind",
            "forward_target", "via", "endpoint", "identity")
    out = {k: state[k] for k in keys if k in state}
    if state.get("role") == "expose":
        out["remote_hint"] = f"agent 端用 serialwrap --endpoint {state.get('endpoint','tcp://127.0.0.1:%d' % state['listen_port'])}"
    return out
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k open_tunnel -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): open_tunnel 編排（durable state、conflict、fail-closed teardown）" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: `status` + `close`（killpg 整組 + verify + orphan scan）

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Consumes: `Registry`、`pid_alive`、`_terminate_pgid`。
- Produces:
  - `def status(run_dir) -> dict`：`{"ok":true,"tunnels":[...]}`；prune 死 state、orphan scan（`cm-*` 對應 ssh 活著卻無 state → 標 `orphan`）。
  - `def close(run_dir, selector) -> dict`：`selector` 為 port（str/int）或 `"all"`；驗 pid+start-ticks → `killpg` 整組 → wait → remove；失敗保留 `status="error"`。回 `{"ok":true,"closed":[...]}`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
def test_status_prunes_dead_and_lists_alive(tmp_path):
    reg = rt.Registry(str(tmp_path))
    live = subprocess.Popen(["sleep", "30"])
    try:
        with reg.lock():
            reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                       "pid": live.pid, "pgid": os.getpgid(live.pid),
                       "pid_start_ticks": rt.read_pid_start_ticks(live.pid),
                       "target": "u@h"})
            reg.write({"listen_port": 7778, "status": "active", "role": "connect",
                       "pid": 999999, "pgid": 999999, "pid_start_ticks": 1, "target": "u@r"})
        res = rt.status(str(tmp_path))
        ports = {t["listen_port"] for t in res["tunnels"]}
        assert 7777 in ports and 7778 not in ports  # 死的被 prune
    finally:
        live.terminate(); live.wait()


def test_close_terminates_and_removes(tmp_path):
    reg = rt.Registry(str(tmp_path))
    live = subprocess.Popen(["sleep", "30"])
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                   "pid": live.pid, "pgid": os.getpgid(live.pid),
                   "pid_start_ticks": rt.read_pid_start_ticks(live.pid), "target": "u@h"})
    res = rt.close(str(tmp_path), 7777)
    assert 7777 in res["closed"]
    assert reg.read(7777) is None
    assert live.poll() is not None  # 已被終止


def test_close_missing_is_idempotent(tmp_path):
    assert rt.close(str(tmp_path), 12345) == {"ok": True, "closed": []}
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "status_prunes or close_" -q`
Expected: FAIL。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
def status(run_dir: str) -> dict:
    reg = Registry(run_dir)
    tunnels: list[dict] = []
    with reg.lock():
        for st in reg.read_all():
            pid = int(st.get("pid", -1))
            if pid_alive(pid, st.get("pid_start_ticks")):
                out = _public(st)
                out["alive"] = True
                tunnels.append(out)
            else:
                reg.remove(int(st["listen_port"]))  # prune 死 state
        # orphan scan：cm-* control socket 活著卻無 state
        try:
            for name in sorted(os.listdir(reg.remote_dir)):
                if name.startswith("cm-") and not reg.read(_port_of_cm(name)):
                    tunnels.append({"status": "orphan", "control_path":
                                    os.path.join(reg.remote_dir, name), "alive": True})
        except OSError:
            pass
    return {"ok": True, "tunnels": tunnels}


def _port_of_cm(name: str) -> int:
    try:
        return int(name[len("cm-"):])
    except ValueError:
        return -1


def close(run_dir: str, selector, *, sleep=time.sleep) -> dict:
    reg = Registry(run_dir)
    closed: list[int] = []
    with reg.lock():
        targets = reg.read_all()
        if str(selector) != "all":
            targets = [s for s in targets if str(s.get("listen_port")) == str(selector)]
        for st in targets:
            port = int(st["listen_port"])
            pid, pgid = int(st.get("pid", -1)), int(st.get("pgid", st.get("pid", -1)))
            if pid_alive(pid, st.get("pid_start_ticks")):
                try:
                    _terminate_pgid(pgid, sleep=sleep)
                except Exception:  # noqa: BLE001
                    st["status"] = "error"
                    reg.write(st)
                    continue
            reg.remove(port)
            closed.append(port)
    return {"ok": True, "closed": closed}
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "status_prunes or close_" -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): status prune/orphan-scan 與 close killpg-verify-error-state" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: real spawner + ssh 探索 + platform guard

**Files:**
- Modify: `sw_core/remote_tunnel.py`
- Test: `tests/test_remote_tunnel.py`

**Interfaces:**
- Produces:
  - `def resolve_ssh_bin(via: str) -> str`：`shutil.which("ssh"/"autossh")`；缺 → `raise TunnelError("SSH_NOT_FOUND")`。
  - `def real_spawner(argv, control_path, log_path) -> (pid, pgid, start_ticks, stderr_tail_fn)`：`subprocess.Popen(argv, start_new_session=True, stdin=DEVNULL, stdout/stderr→log)`；`stderr_tail_fn` 讀 log 尾。
  - `def make_runner() -> callable`：`(argv) -> (rc, stdout+stderr)`，real `subprocess.run`（供 probe）。
  - `def real_ping(endpoint) -> bool`：呼叫 `sw_core.client.rpc_call(endpoint, "health.ping", {}, timeout_s=2.0).get("ok")`。
  - `def guard_platform() -> None`：`os.name == "nt"` → `raise TunnelError("REMOTE_NOT_SUPPORTED")`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_tunnel.py（append）
def test_resolve_ssh_bin_missing(monkeypatch):
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    with pytest.raises(rt.TunnelError) as ei:
        rt.resolve_ssh_bin("ssh")
    assert ei.value.code == "SSH_NOT_FOUND"


def test_guard_platform_rejects_windows(monkeypatch):
    monkeypatch.setattr(rt.os, "name", "nt")
    with pytest.raises(rt.TunnelError) as ei:
        rt.guard_platform()
    assert ei.value.code == "REMOTE_NOT_SUPPORTED"


def test_real_ping_uses_client(monkeypatch):
    seen = {}
    monkeypatch.setattr(rt, "_rpc_call", lambda ep, m, p, timeout_s: seen.setdefault("ep", ep) or {"ok": True})
    assert rt.real_ping("tcp://127.0.0.1:7777") is True
    assert seen["ep"] == "tcp://127.0.0.1:7777"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_tunnel.py -k "resolve_ssh or guard_platform or real_ping" -q`
Expected: FAIL。

- [ ] **Step 3: 寫實作**

```python
# sw_core/remote_tunnel.py（append）
import shutil
import subprocess

from .client import rpc_call as _rpc_call


def guard_platform() -> None:
    if os.name == "nt":
        raise TunnelError("REMOTE_NOT_SUPPORTED",
                          "native Windows 本期不支援 serialwrap remote；請手動 ssh -R（見 SKILL_WINDOWS.md）")


def resolve_ssh_bin(via: str) -> str:
    name = "autossh" if via == "autossh" else "ssh"
    path = shutil.which(name)
    if not path:
        raise TunnelError("SSH_NOT_FOUND", f"PATH 找不到 {name}")
    return path


def real_spawner(argv, control_path, log_path):
    os.makedirs(os.path.dirname(control_path), exist_ok=True)
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115（stderr_tail 需持續讀）
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True,
    )
    pgid = os.getpgid(proc.pid)
    start_ticks = read_pid_start_ticks(proc.pid)

    def _stderr_tail() -> str:
        try:
            with open(log_path, "rb") as fh:
                return fh.read()[-500:].decode("utf-8", "replace")
        except OSError:
            return ""

    return proc.pid, pgid, start_ticks, _stderr_tail


def make_runner():
    def _run(argv):
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=10.0, check=False)
        return proc.returncode, proc.stdout or ""
    return _run


def real_ping(endpoint: str) -> bool:
    try:
        return bool(_rpc_call(endpoint, "health.ping", {}, timeout_s=2.0).get("ok"))
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_tunnel.py -q`
Expected: PASS（全模組 unit 綠）。

- [ ] **Step 5: commit**

```bash
git add sw_core/remote_tunnel.py tests/test_remote_tunnel.py
git commit -m "feat(remote): real spawner／ssh 探索／platform guard／health.ping runner" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 9: CLI `remote` subparser + 分派

**Files:**
- Modify: `sw_core/cli.py`
- Test: `tests/test_remote_cli.py`（Create）

**Interfaces:**
- Consumes: `sw_core.remote_tunnel`（全部）、`sw_core.cli._print`、`_resolve_endpoint`。
- Produces:
  - subparser：`remote`，positional `words`（`nargs="*"`）、`-R`/`-L`（互斥 store_true）、`--autossh`、`--local`（int）、`--socket`、`--remote-socket`、`--ready-timeout`（float，預設 10.0）、`--ssh-opt`（append）。
  - `def _run_remote(args) -> int`：解讀 `words`（`[]`/`status` → status；`close [port|all]` → close；否則 target → open）。open 前 `guard_platform()`、`resolve_ssh_bin`；`-R`/`-L` 互斥檢查（→ `INVALID_ARGS`）；`-R` 預設。以 `_resolve_endpoint(args)`（unix path 或 tcp）推 `forward_src`。組 `TunnelSpec`，呼 `open_tunnel(..., spawner=real_spawner, runner=make_runner(), ping=real_ping)`。全程 `try/except TunnelError` → `_print({"ok":False,"error_code":e.code,...})`、`return 1`。
  - `main()` 加 `if args.cmd == "remote": return _run_remote(args)`。

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_remote_cli.py
from __future__ import annotations

import json
import os

import pytest

from sw_core import cli
from sw_core import remote_tunnel as rt


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def test_remote_bare_is_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote"], capsys)
    assert rc == 0 and obj["ok"] and obj["tunnels"] == []


def test_remote_mutually_exclusive_R_L(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "-R", "-L", "u@h:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_invalid_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "nohost"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_TARGET"


def test_remote_open_dispatches_to_open_tunnel(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    captured = {}
    def fake_open(spec, run_dir, **kw):
        captured["spec"] = spec
        return {"ok": True, "status": "active", "role": "expose", "listen_port": spec.port}
    monkeypatch.setattr(rt, "open_tunnel", fake_open)
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "tester@relay:7777"], capsys)
    assert rc == 0 and obj["status"] == "active"
    assert captured["spec"].role == "expose"  # -R 預設
    assert captured["spec"].ssh_target == "tester@relay"
```

- [ ] **Step 2: 跑 test 確認 fail**

Run: `python3 -m pytest tests/test_remote_cli.py -q`
Expected: FAIL（`remote` subparser 不存在 / `INVALID_ARGS` 未實作）。

- [ ] **Step 3: 寫實作**

在 `build_parser()` 內（與其他 `sub.add_parser` 並列）新增：

```python
    # sw_core/cli.py（build_parser 內）
    p_remote = sub.add_parser(
        "remote",
        help="按需開關 ssh 反向隧道，讓遠端 agent 連本機 daemon（-R 預設 expose）",
        description=(
            "serialwrap remote：外包系統 ssh 建立 -R（expose，把本機 daemon 推到對端）／"
            "-L（connect，relay 情境把對端 port 拉回本機 loopback）隧道，background 常駐。\n"
            "  serialwrap remote user@host:7777        # -R 預設：expose 本機 daemon\n"
            "  serialwrap remote -L user@relay:7777    # connect（relay/雙 NAT）\n"
            "  serialwrap remote                        # 列目前隧道（status）\n"
            "  serialwrap remote close 7777|all         # 拆除\n"
            "安全：只透過 ssh-tunnel、單租戶/可信 relay 或 --remote-socket；不可對網路直接開放。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_remote.add_argument("words", nargs="*", help="[user@]host:port ｜ status ｜ close <port|all>")
    _dir = p_remote.add_mutually_exclusive_group()
    _dir.add_argument("-R", dest="reverse", action="store_true", help="reverse/expose（預設）")
    _dir.add_argument("-L", dest="forward", action="store_true", help="forward/connect（relay）")
    p_remote.add_argument("--autossh", action="store_true", help="以 autossh 斷線自動重連")
    p_remote.add_argument("--local", type=int, default=None, help="-L 本機 loopback port（預設=對端 port）")
    p_remote.add_argument("--remote-socket", dest="remote_socket", default=None,
                          help="硬化：-R 建遠端 unix socket／-L 連該 socket（共享 relay 建議）")
    p_remote.add_argument("--ready-timeout", dest="ready_timeout", type=float, default=10.0,
                          help="readiness 確認上限秒數（逾時回 starting）")
    p_remote.add_argument("--ssh-opt", dest="ssh_opt", action="append", default=[],
                          help="透傳額外 ssh 參數（可重複），如 --ssh-opt=-p --ssh-opt=2222")
    # 註：--socket / --endpoint / --timeout 為既有全域參數，_resolve_endpoint 會取用。
```

新增 handler：

```python
# sw_core/cli.py（新 handler，靠近 _run_skill）
def _run_remote(args: argparse.Namespace) -> int:
    """serialwrap remote 分派：words → status / close / open。"""
    from . import remote_tunnel as rt

    run_dir = _remote_run_dir()
    words = list(getattr(args, "words", []) or [])
    try:
        if not words or words[0] == "status":
            _print(rt.status(run_dir))
            return 0
        if words[0] == "close":
            selector = words[1] if len(words) > 1 else "all"
            _print(rt.close(run_dir, selector))
            return 0

        rt.guard_platform()
        if args.reverse and args.forward:
            raise rt.TunnelError("INVALID_ARGS", "-R 與 -L 不可同時指定")
        role = "connect" if args.forward else "expose"  # -R 預設
        ssh_target, port = rt.parse_target(words[0])
        rt.resolve_ssh_bin("autossh" if args.autossh else "ssh")

        forward_src = None
        if role == "expose":
            forward_src = _forward_src_from_endpoint(_resolve_endpoint(args))

        spec = rt.TunnelSpec(
            role=role, ssh_target=ssh_target, port=port,
            local=args.local, forward_src=forward_src,
            remote_socket=args.remote_socket,
            via="autossh" if args.autossh else "ssh",
            ssh_opts=tuple(args.ssh_opt), ready_timeout=args.ready_timeout,
        )
        res = rt.open_tunnel(
            spec, run_dir,
            spawner=rt.real_spawner, runner=rt.make_runner(), ping=rt.real_ping,
        )
        _print(res)
        return 0
    except rt.TunnelError as exc:
        _print({"ok": False, "error_code": exc.code, "message": exc.message or exc.code})
        return 1


def _forward_src_from_endpoint(endpoint: str) -> str:
    """把 _resolve_endpoint 結果轉成 ssh -R 的本機轉發源。"""
    from .client import _parse_endpoint
    transport, address = _parse_endpoint(endpoint)
    if transport == "unix":
        return address  # AF_UNIX 路徑
    host, tcp_port = address
    return f"127.0.0.1:{tcp_port}"


def _remote_run_dir() -> str:
    """remote state 落在 <run_dir>/remote/。**於呼叫時讀 env**（不可用 import-time
    constants.SOCKET_PATH——測試/隔離以 SERIALWRAP_RUN_DIR 於 import 後覆寫需即時生效）。"""
    run = os.environ.get("SERIALWRAP_RUN_DIR")
    if run and run.strip():
        return os.path.expanduser(run)
    from . import constants
    return constants.RUN_DIR
```

在 `main()` 加分派（與其他 `if args.cmd == ...` 並列）：

```python
    # sw_core/cli.py（main 內）
    if args.cmd == "remote":
        return _run_remote(args)
```

- [ ] **Step 4: 跑 test 確認 pass**

Run: `python3 -m pytest tests/test_remote_cli.py -q`
Expected: PASS。

- [ ] **Step 5: 驗 help 可產生（供 R-16）**

Run: `./serialwrap remote --help`
Expected: 印出 remote 用法，`exit 0`；`./serialwrap --help` 的子命令列表含 `remote`。

- [ ] **Step 6: commit**

```bash
git add sw_core/cli.py tests/test_remote_cli.py
git commit -m "feat(remote): serialwrap remote subparser 與分派（-R 預設、status/close、fail 回 error_code）" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 10: agent 文件（SKILL.md / SKILL_WINDOWS.md）改寫

**Files:**
- Modify: `sw_core/assets/skill/SKILL.md`（「Remote Support 用法（ssh-tunnel）」段，約 line 92-112）
- Modify: `sw_core/assets/skill/SKILL_WINDOWS.md`（新增精簡 remote 段）

**Interfaces:** 無 code interface；內容須與 §3/§4/§8 一致。

- [ ] **Step 1: 改寫 `SKILL.md` 的 Remote Support 段**

把現有教 `ssh -L`（inbound）的段落整段換成（繁中）：

````markdown
## Remote Support 用法（serialwrap remote，ssh-tunnel）

Agent 要從遠端操作本機 UART 時：**在 UART host（daemon 所在機）** 跑一行反向隧道，agent 端照舊用 `--endpoint`。daemon 不重啟、不做預設。

```bash
# UART host（有 serialwrapd）：把本機 daemon 反向推到對端（-R 為預設）
serialwrap remote tester@AGENT_OR_RELAY:7777
```

Agent 端連線（依拓樸擇一）：

- **direct**（agent host 就是上面 ssh 的對端）：直接
  `serialwrap --endpoint tcp://127.0.0.1:7777 session list`
- **relay / 雙 NAT**（agent 與 UART host 互不可達，各自對 relay 撥出）：agent 端先
  `serialwrap remote -L tester@RELAY:7777`（回傳 `endpoint`），再用該 endpoint。

管理：`serialwrap remote`（列隧道）、`serialwrap remote close 7777|all`（拆除）。
回傳 `status`：`active`＝就緒可用；`starting`＝尚未確認（慢速認證／上游未就緒），需再 `remote status` 或重試。

安全：隧道讓對端全權操控 DUT。**只用單租戶／可信 relay**；共享 relay 加 `--remote-socket /path`（遠端改建檔案權限把關的 unix socket）。`-R` tcp 模式若偵測遠端被 `GatewayPorts` 綁到對外，會拒絕（`REMOTE_BIND_UNVERIFIED`）。
````

- [ ] **Step 2: 於 `SKILL_WINDOWS.md` 新增 remote 段**

````markdown
## Remote Support（native Windows：本期不支援 serialwrap remote）

native Windows 執行 `serialwrap remote` 回 `REMOTE_NOT_SUPPORTED`。需遠端存取時**手動**建反向隧道：

```powershell
ssh -N -R 7777:127.0.0.1:48700 user@AGENT_OR_RELAY
```

（Windows daemon 為 TCP loopback `48700`。）agent 端照舊 `serialwrap --endpoint tcp://127.0.0.1:7777`。
````

- [ ] **Step 3: 驗證輸出**

Run: `./serialwrap skill | grep -A2 "serialwrap remote"` 與 `./serialwrap skill --platform windows | grep REMOTE_NOT_SUPPORTED`
Expected: 皆有對應內容。

- [ ] **Step 4: commit**

```bash
git add sw_core/assets/skill/SKILL.md sw_core/assets/skill/SKILL_WINDOWS.md
git commit -m "docs(skill): 改寫 Remote Support 為 serialwrap remote，含 direct/relay/安全/Windows 排除" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 11: README（中英雙語）+ cli-help markers + `.paul-project.yml` + changelog

**Files:**
- Modify: `README.md`（「Remote Support（ssh-tunnel 遠端連線）」段 + cli-help marker 區塊）
- Modify: `.paul-project.yml`（`cli:` 新增 remote 條目）
- Create: `changelog.d/remote-tunnel-cli.md`

**Interfaces:** 無 code；R-16／R-18 對齊。

- [ ] **Step 1: 改寫 README 的「Remote Support」段（繁中 + English 兩份，內容一致）**

以 `serialwrap remote` 為主軸，保留 `ssh -R`/`-L` 手動等價；涵蓋 direct/relay、`--remote-socket` 硬化、單租戶 relay 警語、`status`/`close`、`starting` 語意、`REMOTE_BIND_UNVERIFIED`／`REMOTE_NOT_SUPPORTED`。英文段為繁中段對照翻譯（依語言政策雙語並存）。

- [ ] **Step 2: `.paul-project.yml` 新增 cli 條目**

```yaml
  - command: "./serialwrap remote"
    help_args: ["--help"]
    reflected_in: "README.md"
    marker: "serialwrap-remote-help"
```

- [ ] **Step 3: README 插入／再生 cli-help marker 區塊**

新增：
```markdown
<!-- BEGIN: cli-help marker="serialwrap-remote-help" -->
```
（貼上 `LC_ALL=C ./serialwrap remote --help` 的輸出）
```markdown
<!-- END: cli-help marker="serialwrap-remote-help" -->
```
並**再生** `serialwrap-help` 區塊（`remote` 已入子命令列表）：把 `LC_ALL=C ./serialwrap --help` 輸出貼回既有 `serialwrap-help` marker 之間。

Run（取得權威 help 文字）：
```bash
LC_ALL=C ./serialwrap --help
LC_ALL=C ./serialwrap remote --help
```

- [ ] **Step 4: 新增 changelog fragment**

```bash
cat > changelog.d/remote-tunnel-cli.md <<'MD'
### Added
- `serialwrap remote`：按需開關 SSH 反向隧道（`-R` expose 預設／`-L` connect），讓遠端 agent 取得本機 serialwrap 操作；background 常駐、flock registry、readiness 確認、`--remote-socket` 硬化、fail-closed 遠端 bind 驗證。daemon 零改動、不做預設、僅 POSIX（native Windows 回 `REMOTE_NOT_SUPPORTED`）。
MD
```

- [ ] **Step 5: 驗 R-16 對齊**

Run: `python3 -m policy_check --repo . 2>&1 | grep -Ei "R-16|remote-help" || echo "R-16 ok"`
Expected: R-16 PASS（marker 與 `--help` 輸出一致）。

- [ ] **Step 6: commit**

```bash
git add README.md .paul-project.yml changelog.d/remote-tunnel-cli.md
git commit -m "docs(readme): Remote Support 改寫為 serialwrap remote（雙語）+ R-16 cli-help + changelog" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 12: docker 三拓樸驗收 harness

**Files:**
- Create: `tools/docker/remote_tunnel_test.sh`
- Modify: `Dockerfile`（或 `tools/docker/` 內既有 build 定義）——測試映像加 `openssh-server`／`openssh-client`／`autossh`／`iproute2` + 預燒 keypair
- Reference: `tools/docker/remote_smoke.sh`（既有模式）、`tools/docker/remote_lab.py`

**Interfaces:** 產出可執行的 acceptance gate；退出碼 0＝全拓樸通過。

- [ ] **Step 1: 擴充測試映像**

於 build 的套件安裝加 `openssh-server openssh-client autossh iproute2`；生成一組測試 keypair 並寫入 `authorized_keys`（passwordless），relay/agent sshd 設定 `GatewayPorts no`（另備一個 `GatewayPorts yes` 的 sshd 設定供 fail-closed 案例）。

- [ ] **Step 2: 寫 `remote_tunnel_test.sh`（骨架，逐拓樸驗收）**

腳本須做（對照 spec §11.2）：
1. build 映像、建 docker networks（`net_direct`、`net_a`、`net_b`）。
2. **拓樸 1 direct**：起 `uart`（serialwrapd）、`agent`（sshd）同 `net_direct`。斷言①預設不啟用（`remote status` 空、無 state、attacker 連 `uart` 之外的 endpoint 失敗）；`uart` 跑 `serialwrap remote tester@agent:7777`；`agent` 用 `--endpoint tcp://127.0.0.1:7777` 跑 `session list`＋`cmd submit`→`done`；斷言③ daemon pid 不變；④ `close all` 後復歸。
3. **拓樸 2 NAT→host**：`uart`＋`relay` 同 `net_a`；`uart` `remote -R tester@relay:7777`；relay 上 agent CLI 用 loopback；同斷言。
4. **拓樸 3 NAT←client**：`net_a`=`uart`+`relay`、`net_b`=`agent`+`relay`；tcp 模式與 `--remote-socket` 硬化模式各跑一次端到端；同斷言。
5. **斷言⑤ loopback 不變量**：relay 上 `ss -ltn` 該 port 綁 `127.0.0.1`；獨立 attacker 容器連 `relay:7777` 失敗。
6. **斷言⑧ GatewayPorts fail-closed**：對 `GatewayPorts yes` 的 relay，`-R` tcp 模式回 `REMOTE_BIND_UNVERIFIED` 且無殘留（`remote status` 空、無 ssh 行程）；同情境 `--remote-socket` 仍成功。
7. **斷言⑨ -L 端到端**：relay 無對應 `-R` 時 `remote -L` 回 `starting`（非 active）。
8. `trap cleanup EXIT` 移除容器與 networks。任何斷言失敗即 `exit 1`。

（可沿用 `remote_smoke.sh` 的 `docker run`／`json_extract` 輔助結構。）

- [ ] **Step 3: 本機執行驗收（需 docker）**

Run: `./tools/docker/remote_tunnel_test.sh`
Expected: 三拓樸全過，最後印 `[serialwrap] remote-tunnel acceptance: PASS`，`exit 0`。
（docker 不可用環境：腳本開頭偵測 `docker` 不存在 → 印原因並 `exit 0`（SKIP，不靜默略過），CI 對應標記。）

- [ ] **Step 4: commit**

```bash
git add tools/docker/remote_tunnel_test.sh Dockerfile
git commit -m "test(remote): docker 三拓樸驗收（direct/NAT→host/NAT←client）含預設不啟用/fail-closed/端到端" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 13: 全面驗證 + 收尾

**Files:** 無新增；跑政策與測試。

- [ ] **Step 1: 全測試**

Run: `python3 -m pytest -q tests/`
Expected: 無新失敗（僅既有已知 `test_five_agents_three_rounds_no_conflict` 可能失敗）。

- [ ] **Step 2: policy check**

Run: `python3 -m policy_check --repo .`
Expected: PASS（R-09 fragment、R-16 cli-help、R-18 docs 對齊、R-13/R-14 symlink）。

- [ ] **Step 3: docker 驗收（若環境可用）**

Run: `./tools/docker/remote_tunnel_test.sh`
Expected: PASS 或明確 SKIP（docker 不可用）。

- [ ] **Step 4: 收尾**

依 `superpowers:finishing-a-development-branch` 決定 merge/PR；PR body 填 `.github/pull_request_template.md` 的 Policy Checklist，附 `🤖 Generated with [Claude Code]` 與 session 連結。無對應 issue，故不寫 closing-keyword（或視需要上 `policy-exempt:issue-link`）。

---

## Self-Review（計畫對 spec 覆蓋）

- §3 介面（`-R` 預設/`-L`/status/close/旗標）→ Task 9。
- §4.0 identity＋readiness → Tasks 1、4、5、6。
- §4.1 expose（forward_src、conflict、fail-closed bind 驗證）→ Tasks 2、5、6。
- §4.2 connect（remote-socket 配對、health.ping readiness）→ Tasks 2、5、6。
- §4.4/§4.5 status/close（pgid、verify、orphan、error-state）→ Task 7。
- §5 argv（BatchMode/ControlMaster/loopback bind/autossh）→ Task 2。
- §6 registry（flock、durable、pid_start_ticks）→ Tasks 3、6。
- §7 `_resolve_endpoint` 重用 → Task 9（`_forward_src_from_endpoint`）。
- §8 安全（trust boundary、GatewayPorts fail-closed、BatchMode）→ Tasks 2、5、文件 10/11。
- §9 error codes（含 `REMOTE_BIND_UNVERIFIED`／`REMOTE_NOT_SUPPORTED`／`TUNNEL_CONFLICT`）→ Tasks 5-9。
- §10 改動表面 → Tasks 9（cli）、1-8（module）、10-11（docs）。
- §11 測試（unit + docker 三拓樸 + 斷言 1-9）→ Tasks 1-8（unit）、12（docker）。
- §12 相依（ssh/OpenSSH、/proc）→ Tasks 3、8。
- Windows 排除（§2/§5/§13）→ Task 8（guard）、9（分派）、10（SKILL_WINDOWS）。

型別一致性：`TunnelSpec`／`TunnelError`／`Registry`／`compute_identity`／`build_argv`／`wait_ready`／`open_tunnel`／`status`／`close` 跨 Task 命名一致。
