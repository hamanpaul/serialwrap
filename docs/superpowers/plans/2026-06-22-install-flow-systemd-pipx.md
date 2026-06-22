# serialwrap 安裝流程（pipx + systemd，on-demand 降級）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 serialwrap 從「`install.sh` copy + 手動 PATH + 裸借系統 PyYAML + 按需自啟」轉為「pipx + git+SHA 安裝、systemd 服務為主、無 systemd 退回 on-demand」的正規可發佈安裝。

**Architecture:** 先把 daemon 主程式與資產搬進可打包的 `sw_core` 套件並加 `pyproject.toml`（Phase 1）；路徑改 XDG 且保留 env 覆寫（Phase 2）；以可注入的 effects 介面實作監管模式 gate（Phase 3）與 systemd unit/service 子命令（Phase 4）；`setup`/`doctor` reconciler 串起全部（Phase 5）；最後對齊 install.sh/Dockerfile/README/CI（Phase 6）。

**Tech Stack:** Python ≥3.10（stdlib termios/asyncio + PyYAML）、setuptools/pyproject、pipx、systemd（user/system unit）、pytest/unittest。

**參考：** 設計 `docs/superpowers/specs/2026-06-22-install-flow-systemd-pipx-design.md`；需求 `openspec/changes/install-flow-systemd-pipx/specs/**`。每個 Phase 結束跑 `python3 -m pytest -q tests/` 確認無新失敗（既知 pre-existing flaky：`test_multiagent_e2e::test_five_agents_three_rounds_no_conflict`、負載型 `test_flash_pump::test_rx_writer_writes_to_master`）。

---

## Phase 1 — 打包基礎

### Task 1: daemon 主程式搬進套件 + root 薄 shim

**Files:**
- Create: `sw_core/daemon.py`
- Modify: `serialwrapd.py`（整檔改寫為 shim）
- Test: `tests/test_issue52_rpc_concurrency.py`（既有，驗證 `serialwrapd.BLOCKING_RPC_METHODS` 仍可匯入）

- [ ] **Step 1: 寫失敗測試** — 新增 `tests/test_packaging_entrypoints.py`

```python
import importlib
def test_daemon_main_importable_from_package():
    mod = importlib.import_module("sw_core.daemon")
    assert callable(mod.main)
    assert {"file.push", "file.pull"} <= set(mod.BLOCKING_RPC_METHODS)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_packaging_entrypoints.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'sw_core.daemon'`）

- [ ] **Step 3: 建 `sw_core/daemon.py`** — 把現有 root `serialwrapd.py` 內容整段搬入，import 改為套件內相對/絕對引用，並保留 `BLOCKING_RPC_METHODS`：

```python
from __future__ import annotations
import argparse, asyncio, signal, sys
from sw_core.config import load_profiles
from sw_core.constants import LOCK_PATH, PROFILE_DIR, SOCKET_PATH, ensure_runtime_dirs
from sw_core.daemon_lock import SingletonLock
from sw_core.rpc import JsonRpcUnixServer
from sw_core.service import SerialwrapService

BLOCKING_RPC_METHODS = {"file.push", "file.pull"}

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="serialwrapd", description="serialwrap daemon")
    p.add_argument("--profile-dir", default=PROFILE_DIR)
    p.add_argument("--socket", default=SOCKET_PATH)
    p.add_argument("--lock", default=LOCK_PATH)
    return p

async def _run_async(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    result = load_profiles(args.profile_dir)
    if not result.profiles and not result.templates:
        sys.stderr.write("serialwrapd: no profiles loaded\n")
    lock = SingletonLock(args.lock, args.socket)
    try:
        lock.acquire()
    except RuntimeError as exc:
        sys.stderr.write(f"serialwrapd: {exc}\n")
        return 2
    service = SerialwrapService(result.profiles, templates=result.templates, max_sessions=result.max_sessions)
    stop_event = asyncio.Event()
    def _handle(method, params):
        if method == "daemon.stop":
            stop_event.set()
            return {"ok": True, "stopping": True}
        return service.rpc(method, params)
    server = JsonRpcUnixServer(args.socket, _handle, blocking_methods=BLOCKING_RPC_METHODS)
    def _stop(*_):
        stop_event.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    try:
        service.start()
        await server.start()
        await stop_event.wait()
    finally:
        await server.stop()
        service.stop()
        lock.release()
    return 0

def main(argv: list[str] | None = None) -> int:
    return int(asyncio.run(_run_async(build_parser().parse_args(argv))))
```

- [ ] **Step 4: root `serialwrapd.py` 改為 shim**

```python
#!/usr/bin/env python3
from sw_core.daemon import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑測試確認通過 + 全測試不破**

Run: `python3 -m pytest tests/test_packaging_entrypoints.py tests/test_issue52_rpc_concurrency.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sw_core/daemon.py serialwrapd.py tests/test_packaging_entrypoints.py
git commit -m "refactor(daemon): 主程式搬進 sw_core.daemon，root serialwrapd.py 轉 shim"
```

### Task 2: relocate 資產進套件 + importlib.resources 取用器

**Files:**
- Move: `profiles/` → `sw_core/assets/profiles/`、`tools/` → `sw_core/assets/tools/`、`skills/serialwrap/` → `sw_core/assets/skill/`
- Create: `sw_core/assets/__init__.py`
- Modify: 引用舊路徑處（`sw_core/constants.py` 的 `PROFILE_DIR` 由 Task 4 處理；本任務先確保 resources 可讀）
- Test: `tests/test_packaging_entrypoints.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_bundled_assets_readable_via_resources():
    from sw_core import assets
    names = assets.list_profile_files()
    assert any(n.endswith(".yaml") for n in names)
    assert "minicom_router.sh" in assets.list_tool_files()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_packaging_entrypoints.py::test_bundled_assets_readable_via_resources -q`
Expected: FAIL（`ImportError`／`assets` 無此函式）

- [ ] **Step 3: git mv 資產 + 建取用器**

```bash
git mv profiles sw_core/assets/profiles
git mv tools sw_core/assets/tools
mkdir -p sw_core/assets && git mv skills/serialwrap sw_core/assets/skill
touch sw_core/assets/__init__.py
```

`sw_core/assets/__init__.py`：

```python
from __future__ import annotations
from importlib import resources

def _names(subdir: str) -> list[str]:
    root = resources.files(__package__) / subdir
    return sorted(p.name for p in root.iterdir() if p.is_file())

def list_profile_files() -> list[str]:
    return _names("profiles")

def list_tool_files() -> list[str]:
    return _names("tools")

def copy_tree(subdir: str, dest) -> None:
    """把套件內某子目錄遞迴複製到 dest（materialize 用）。"""
    import shutil, pathlib
    src = resources.files(__package__) / subdir
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            with resources.as_file(item) as real:
                shutil.copytree(real, target, dirs_exist_ok=True)
        else:
            with resources.as_file(item) as real:
                shutil.copy2(real, target)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_packaging_entrypoints.py -q`
Expected: PASS

- [ ] **Step 5: 修既有對舊路徑的硬引用** — `git grep -n "skills/serialwrap\|^profiles/\|\"tools/\|'tools/"` 找出 `install.sh`/func-test/README 等引用（install.sh/Dockerfile/README 在 Phase 6 統一改；func-test 若用到 `profiles/` 改指 `sw_core/assets/profiles`）。跑 `python3 -m pytest -q tests/` 確認無新失敗。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(assets): profiles/tools/skill relocate 進 sw_core/assets，加 importlib.resources 取用器"
```

### Task 3: 新增 pyproject.toml

**Files:**
- Create: `pyproject.toml`
- Test: `tests/test_packaging_entrypoints.py`

- [ ] **Step 1: 寫失敗測試**（驗證 metadata 可被讀且 entry points 宣告正確）

```python
def test_pyproject_declares_entrypoints_and_deps():
    import tomllib, pathlib
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["serialwrap"] == "sw_core.cli:main"
    assert scripts["serialwrapd"] == "sw_core.daemon:main"
    assert any(d.lower().startswith("pyyaml") for d in data["project"]["dependencies"])
    assert data["project"]["requires-python"] == ">=3.10"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_packaging_entrypoints.py::test_pyproject_declares_entrypoints_and_deps -q`
Expected: FAIL（`FileNotFoundError: pyproject.toml`）

- [ ] **Step 3: 建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "serialwrap"
dynamic = ["version"]
description = "UART broker daemon + CLI（多 agent/human 共用序列埠）"
requires-python = ">=3.10"
dependencies = ["PyYAML>=6"]

[project.scripts]
serialwrap = "sw_core.cli:main"
serialwrapd = "sw_core.daemon:main"

[tool.setuptools]
packages = ["sw_core", "sw_core.event_engine", "sw_core.assets"]

[tool.setuptools.package-data]
"sw_core.assets" = ["profiles/*", "tools/*", "skill/**/*"]

[tool.setuptools.dynamic]
version = { file = ["VERSION"] }
```

- [ ] **Step 4: 跑測試 + 乾淨 venv 安裝 smoke**

Run:
```bash
python3 -m pytest tests/test_packaging_entrypoints.py -q
python3 -m venv /tmp/sw_pkgtest && /tmp/sw_pkgtest/bin/pip install -q . \
  && /tmp/sw_pkgtest/bin/serialwrap --help >/dev/null && echo OK_ENTRYPOINTS
```
Expected: PASS + `OK_ENTRYPOINTS`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_packaging_entrypoints.py
git commit -m "build: 新增 pyproject.toml（setuptools，serialwrap/serialwrapd entry points，PyYAML，py>=3.10）"
```

---

## Phase 2 — 路徑/XDG（runtime-paths）

### Task 4: constants 改 XDG，保留 env 覆寫

**Files:**
- Modify: `sw_core/constants.py`
- Test: `tests/test_runtime_paths_xdg.py`（新增）

- [ ] **Step 1: 寫失敗測試**

```python
import importlib, os
def _reload(env, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("SERIALWRAP_", "XDG_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import sw_core.constants as c
    return importlib.reload(c)

def test_user_defaults_use_xdg_not_tmp(monkeypatch, tmp_path):
    c = _reload({"XDG_RUNTIME_DIR": str(tmp_path/"run"),
                 "XDG_STATE_HOME": str(tmp_path/"state"),
                 "XDG_CONFIG_HOME": str(tmp_path/"cfg")}, monkeypatch)
    assert "/tmp" not in c.SOCKET_PATH
    assert c.SOCKET_PATH.startswith(str(tmp_path/"run"))
    assert c.STATE_PATH.startswith(str(tmp_path/"state"))

def test_env_override_wins(monkeypatch, tmp_path):
    c = _reload({"SERIALWRAP_RUN_DIR": str(tmp_path/"x")}, monkeypatch)
    assert c.SOCKET_PATH == str(tmp_path/"x"/"serialwrapd.sock")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_runtime_paths_xdg.py -q`
Expected: FAIL（現預設為 `/tmp/serialwrap`）

- [ ] **Step 3: 改寫 `sw_core/constants.py` 路徑解析**（保留 `_env_path`，新增 XDG 預設；env 仍優先）

```python
def _xdg(name: str, fallback: str) -> str:
    return os.environ.get(name) or os.path.join(os.path.expanduser("~"), fallback)

_CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")
_STATE_HOME = _xdg("XDG_STATE_HOME", ".local/state")
_DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
_RUNTIME_HOME = os.environ.get("XDG_RUNTIME_DIR")  # 可能 None

CONFIG_DIR = _env_path("SERIALWRAP_CONFIG_DIR", os.path.join(_CONFIG_HOME, "serialwrap"))
STATE_DIR = _env_path("SERIALWRAP_STATE_DIR", os.path.join(_STATE_HOME, "serialwrap"))
RUN_DIR = _env_path(
    "SERIALWRAP_RUN_DIR",
    os.path.join(_RUNTIME_HOME, "serialwrap") if _RUNTIME_HOME else os.path.join(STATE_DIR, "run"),
)
LOCK_PATH = os.path.join(RUN_DIR, "serialwrapd.lock")
SOCKET_PATH = os.path.join(RUN_DIR, "serialwrapd.sock")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
WAL_DIR = _env_path("SERIALWRAP_WAL_DIR", os.path.join(STATE_DIR, "wal"))
PROFILE_DIR = _env_path("SERIALWRAP_PROFILE_DIR", os.path.join(CONFIG_DIR, "profiles"))
DATA_DIR = _env_path("SERIALWRAP_DATA_DIR", os.path.join(_DATA_HOME, "serialwrap"))
```

（`ensure_runtime_dirs()` 補建 `CONFIG_DIR`/`RUN_DIR`/`STATE_DIR`。）

- [ ] **Step 4: 跑測試確認通過 + 全測試**

Run: `python3 -m pytest tests/test_runtime_paths_xdg.py -q && python3 -m pytest -q tests/`
Expected: PASS（無新失敗；既有測試多以 env 覆寫隔離，故不受預設改變影響）

- [ ] **Step 5: Commit**

```bash
git add sw_core/constants.py tests/test_runtime_paths_xdg.py
git commit -m "feat(paths): 路徑改 XDG 預設（脫離 /tmp），保留 SERIALWRAP_* 覆寫優先"
```

### Task 5: config.yaml（supervision_mode + 有效 socket）與 state 遷移

**Files:**
- Create: `sw_core/runtime_config.py`、`sw_core/state_migrate.py`
- Test: `tests/test_runtime_config.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_config_roundtrip(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    rc = RuntimeConfig(tmp_path/"config.yaml")
    rc.set_mode("systemd-user", socket_path="/run/user/1000/serialwrap/serialwrapd.sock")
    rc2 = RuntimeConfig(tmp_path/"config.yaml")
    assert rc2.mode() == "systemd-user"
    assert rc2.socket_path().endswith("serialwrapd.sock")

def test_state_migrate_only_when_dest_empty(tmp_path):
    from sw_core.state_migrate import migrate_legacy_state
    legacy = tmp_path/"old"; legacy.mkdir(); (legacy/"state.json").write_text('{"x":1}')
    dest = tmp_path/"new"/"state.json"
    assert migrate_legacy_state(legacy/"state.json", dest) is True
    assert dest.read_text() == '{"x":1}'
    assert migrate_legacy_state(legacy/"state.json", dest) is False  # dest 已存在不再搬
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_runtime_config.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `sw_core/runtime_config.py`**

```python
from __future__ import annotations
import os, yaml
from pathlib import Path

class RuntimeConfig:
    def __init__(self, path):
        self._path = Path(path)
        self._data = {}
        if self._path.exists():
            self._data = yaml.safe_load(self._path.read_text()) or {}

    def mode(self) -> str | None:
        return self._data.get("supervision_mode")

    def socket_path(self) -> str | None:
        return self._data.get("socket_path")

    def set_mode(self, mode: str, *, socket_path: str | None = None) -> None:
        self._data["supervision_mode"] = mode
        if socket_path is not None:
            self._data["socket_path"] = socket_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump(self._data, allow_unicode=True))
```

`sw_core/state_migrate.py`：

```python
from __future__ import annotations
import shutil
from pathlib import Path

def migrate_legacy_state(legacy: Path, dest: Path) -> bool:
    legacy, dest = Path(legacy), Path(dest)
    if not legacy.exists() or dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, dest)
    return True
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_runtime_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/runtime_config.py sw_core/state_migrate.py tests/test_runtime_config.py
git commit -m "feat(config): 新增 RuntimeConfig（supervision_mode/有效 socket）與 legacy state 遷移"
```

---

## Phase 3 — 監管核心（effects 介面 + auto-spawn gate）

### Task 6: 可注入 effects 介面

**Files:**
- Create: `sw_core/sysenv.py`
- Test: `tests/test_sysenv.py`

- [ ] **Step 1: 寫失敗測試**（驗證可注入 fake、預設實作存在）

```python
def test_fake_effects_records_calls():
    from sw_core.sysenv import FakeEffects
    fx = FakeEffects(systemd=True, in_dialout=False, commands={"systemctl --user is-active serialwrap": (0,"active","")})
    assert fx.has_systemd() is True
    assert fx.user_in_group("dialout") is False
    rc, out, err = fx.run(["systemctl","--user","is-active","serialwrap"])
    assert rc == 0 and out == "active"
    assert ["systemctl","--user","is-active","serialwrap"] in fx.calls
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_sysenv.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `sw_core/sysenv.py`** — 一個 `Effects` 協定 + 真實 `SystemEffects`（包 `subprocess`/`os`/`grp`/檔案）＋ `FakeEffects`（測試用，依 `commands` 表回傳、記錄 `calls`）。介面方法：`run(cmd)->(rc,out,err)`、`has_systemd()->bool`、`user_in_group(g)->bool`、`write_file/symlink/exists/copy_tree`、`is_wsl()->bool`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_sysenv.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/sysenv.py tests/test_sysenv.py
git commit -m "feat(sysenv): 可注入 effects 介面（SystemEffects/FakeEffects）"
```

### Task 7: auto-spawn gate（CLI 與 minicom_router 讀 supervision_mode）

**Files:**
- Modify: `sw_core/cli.py`（連線前的 lazy-start 判斷）、`sw_core/assets/tools/minicom_router.sh`
- Test: `tests/test_autospawn_gate.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_cli_does_not_spawn_in_systemd_mode(tmp_path, monkeypatch):
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path/"config.yaml"); rc.set_mode("systemd-user")
    assert should_auto_spawn(rc) is False

def test_cli_spawns_in_on_demand_mode(tmp_path):
    from sw_core.runtime_config import RuntimeConfig
    from sw_core.cli import should_auto_spawn
    rc = RuntimeConfig(tmp_path/"config.yaml"); rc.set_mode("on-demand")
    assert should_auto_spawn(rc) is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_autospawn_gate.py -q`
Expected: FAIL（`should_auto_spawn` 不存在）

- [ ] **Step 3: 在 `sw_core/cli.py` 加 gate 函式並接到 lazy-start 路徑**

```python
def should_auto_spawn(rc) -> bool:
    return (rc.mode() or "on-demand") == "on-demand"
```

連線失敗時：`should_auto_spawn` 為 False → 回明確錯誤訊息（提示 `serialwrap service start`），不 spawn；為 True → 維持既有 spawn。`minicom_router.sh` 在 `_ensure_daemon` 前讀 `config.yaml` 的 `supervision_mode`，systemd 模式改呼叫 `serialwrap service start` 或報錯而非 `daemon start`。

- [ ] **Step 4: 跑測試確認通過 + 全測試**

Run: `python3 -m pytest tests/test_autospawn_gate.py -q && python3 -m pytest -q tests/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/cli.py sw_core/assets/tools/minicom_router.sh tests/test_autospawn_gate.py
git commit -m "feat(supervision): systemd 模式 gate 掉 auto-spawn，避免雙 daemon 競態"
```

---

## Phase 4 — systemd unit 與 service 子命令

### Task 8: unit 範本產生器（user/system，禁 /dev 沙箱）

**Files:**
- Create: `sw_core/systemd_units.py`
- Test: `tests/test_systemd_units.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_user_unit_has_no_device_sandbox_and_restart():
    from sw_core.systemd_units import render_user_unit
    text = render_user_unit(exec_start="%h/.local/bin/serialwrapd")
    assert "Restart=on-failure" in text
    assert "PrivateDevices" not in text and "DeviceAllow" not in text

def test_system_unit_runs_service_account_in_dialout():
    from sw_core.systemd_units import render_system_unit
    text = render_system_unit(exec_start="/usr/local/bin/serialwrapd --socket /run/serialwrap/serialwrapd.sock")
    assert "User=serialwrap" in text
    assert "SupplementaryGroups=dialout" in text
    assert "RuntimeDirectory=serialwrap" in text
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_systemd_units.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `sw_core/systemd_units.py`**（兩個 render 函式回傳 §7 設計的 unit 文字；以 f-string 帶入 `exec_start`，明確不含任何 `PrivateDevices`/`DeviceAllow`）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_systemd_units.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/systemd_units.py tests/test_systemd_units.py
git commit -m "feat(systemd): user/system unit 範本產生器（Restart、禁 /dev 沙箱）"
```

### Task 9: `serialwrap service` 子命令包 systemctl

**Files:**
- Create: `sw_core/service_ctl.py`
- Modify: `sw_core/cli.py`（註冊 `service` 子命令；systemd 模式下 `daemon stop` 重導）
- Test: `tests/test_service_ctl.py`

- [ ] **Step 1: 寫失敗測試**（用 FakeEffects 斷言下對 systemctl 參數）

```python
def test_service_restart_user_mode_calls_systemctl_user():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(commands={"systemctl --user restart serialwrap": (0,"","")})
    service_action("restart", mode="systemd-user", fx=fx)
    assert ["systemctl","--user","restart","serialwrap"] in fx.calls

def test_service_start_system_mode_uses_sudo_systemctl():
    from sw_core.sysenv import FakeEffects
    from sw_core.service_ctl import service_action
    fx = FakeEffects(commands={"sudo systemctl start serialwrap": (0,"","")})
    service_action("start", mode="systemd-system", fx=fx, with_sudo=True)
    assert ["sudo","systemctl","start","serialwrap"] in fx.calls
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_service_ctl.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `sw_core/service_ctl.py`** — `service_action(action, *, mode, fx, with_sudo=False)`：user 模式 → `systemctl --user <action> serialwrap`；system 模式 → `systemctl <action> serialwrap`（需 root 動作預設印指令，`with_sudo` 才前綴 `sudo`）；on-demand 模式 → 回明確訊息（無 systemd）。在 `cli.py` 註冊 `service {start|stop|restart|status}`，並讓 systemd 模式下 `daemon stop` 呼叫 `service_action("stop", ...)`。

- [ ] **Step 4: 跑測試確認通過 + 全測試**

Run: `python3 -m pytest tests/test_service_ctl.py -q && python3 -m pytest -q tests/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/service_ctl.py sw_core/cli.py tests/test_service_ctl.py
git commit -m "feat(cli): serialwrap service 子命令包 systemctl；systemd 模式 daemon stop 重導"
```

---

## Phase 5 — setup / doctor reconciler

### Task 10: 資產物化（materialize）

**Files:**
- Create: `sw_core/setup_cmd.py`（先放 materialize 部分）
- Test: `tests/test_setup_materialize.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_materialize_copies_profiles_and_symlinks_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path/"cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path/"data"))
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)
    assert (tmp_path/"cfg"/"serialwrap"/"profiles"/"default.yaml").exists()
    assert (tmp_path/".agents"/"skills"/"serialwrap").is_symlink()

def test_materialize_does_not_overwrite_existing_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path/"cfg"))
    p = tmp_path/"cfg"/"serialwrap"/"profiles"/"default.yaml"; p.parent.mkdir(parents=True); p.write_text("MINE")
    from sw_core.setup_cmd import materialize_assets
    materialize_assets(home=tmp_path)            # 無 force
    assert p.read_text() == "MINE"
    materialize_assets(home=tmp_path, force=True) # force 才覆寫
    assert p.read_text() != "MINE"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_setup_materialize.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `materialize_assets`** — 用 `sw_core.assets.copy_tree` 把 profiles 拷到 `CONFIG_DIR/profiles`（存在且非 force 則跳過）、skill 拷到 `DATA_DIR/skill` 並 symlink `~/.agents/skills/serialwrap`、minicom wrappers 拷到 `~/.local/bin`（檔頭確保 `command -v minicom`）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_setup_materialize.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/setup_cmd.py tests/test_setup_materialize.py
git commit -m "feat(setup): materialize profiles/skill/minicom 到使用者可寫位置（不覆蓋/--force）"
```

### Task 11: setup reconciler（模式決策 + 轉換先停舊再起新 + flash 護欄 + sudo 邊界 + WSL 引導）

**Files:**
- Modify: `sw_core/setup_cmd.py`
- Test: `tests/test_setup_reconcile.py`

- [ ] **Step 1: 寫失敗測試**（全走 FakeEffects；驗證轉換順序與護欄）

```python
def test_transition_stops_old_before_starting_new(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    # 舊 on-demand 有 daemon 在跑；目標 systemd-user
    res = reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx,
                    daemon_running=True, any_flashing=False, home=tmp_path)
    order = [c for c in fx.calls if "stop" in " ".join(c) or "start" in " ".join(c) or "daemon" in " ".join(c)]
    # 先出現停舊（daemon stop），再出現起新（systemctl --user start）
    assert any("stop" in " ".join(c) for c in order)
    assert order.index(next(c for c in order if "stop" in " ".join(c))) \
         < order.index(next(c for c in order if "start" in " ".join(c)))
    assert res["mode"] == "systemd-user"

def test_transition_aborts_when_flashing(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile, FlashingBusy
    fx = FakeEffects(systemd=True)
    try:
        reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx,
                  daemon_running=True, any_flashing=True, home=tmp_path)
        assert False, "should raise"
    except FlashingBusy:
        pass

def test_idempotent_same_mode_no_teardown(tmp_path):
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    reconcile(old_mode="systemd-user", target_mode="systemd-user", fx=fx,
              daemon_running=True, any_flashing=False, home=tmp_path)
    assert not any("stop" in " ".join(c) for c in fx.calls)  # 不拆
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_setup_reconcile.py -q`
Expected: FAIL（`reconcile`/`FlashingBusy` 不存在）

- [ ] **Step 3: 實作 `reconcile`**：
  1. `any_flashing` 為真 → raise `FlashingBusy`（除非 `force`）。
  2. `target == old` → 冪等刷新（materialize、確保 unit enabled），不停舊。
  3. `target != old` → 先停舊（on-demand→`serialwrapd daemon.stop`/kill；systemd→`systemctl [--user] stop/disable`）；必要時 `migrate_legacy_state`；起新（裝 unit + enable/linger 或設 on-demand）；寫 `RuntimeConfig.set_mode`。
  4. 回 `{"mode": target, ...}`。需要 root 的動作經 `with_sudo` 控制（預設只蒐集「待執行指令」回傳給呼叫者印出）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python3 -m pytest tests/test_setup_reconcile.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/setup_cmd.py tests/test_setup_reconcile.py
git commit -m "feat(setup): reconciler（模式轉換先停舊再起新、flash 護欄、sudo 邊界）"
```

### Task 12: doctor + legacy 遷移 + 接到 CLI

**Files:**
- Create: `sw_core/doctor_cmd.py`
- Modify: `sw_core/cli.py`（註冊 `setup`/`doctor`）、`install.sh`（Phase 6）
- Test: `tests/test_doctor.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_doctor_reports_dialout_missing_with_fix():
    from sw_core.sysenv import FakeEffects
    from sw_core.doctor_cmd import run_doctor
    fx = FakeEffects(systemd=True, in_dialout=False)
    report = run_doctor(fx=fx)
    item = next(i for i in report if i["check"] == "dialout")
    assert item["ok"] is False
    assert "usermod -aG dialout" in item["fix"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_doctor.py -q`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 實作 `run_doctor(fx)`** 回傳逐項 `{check, ok, detail, fix}`（python 版本/PyYAML/PATH/dialout/systemd+unit/supervision_mode/socket/裝置/WSL 旗標）；在 `cli.py` 註冊 `serialwrap setup`（呼叫 materialize+reconcile，偵測 legacy `~/.paul_tools` 並印退役引導）與 `serialwrap doctor`（印 report）。

- [ ] **Step 4: 跑測試確認通過 + 全測試**

Run: `python3 -m pytest tests/test_doctor.py -q && python3 -m pytest -q tests/`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sw_core/doctor_cmd.py sw_core/cli.py tests/test_doctor.py
git commit -m "feat(cli): serialwrap setup/doctor 子命令（含 legacy ~/.paul_tools 引導）"
```

---

## Phase 6 — 入口/文件/CI 對齊

### Task 13: install.sh / Dockerfile / README

**Files:**
- Modify: `install.sh`、`Dockerfile`、`README.md`

- [ ] **Step 1: `install.sh` 轉型**（dev/本地：`pipx install "$SCRIPT_DIR" && serialwrap setup`；保留 `--legacy` 走舊 copy 行為並標 deprecated）

- [ ] **Step 2: `Dockerfile`** 改 `pip install .`（移除 `pyyaml pyserial` 手裝改由 pyproject 帶 PyYAML、刪 pyserial），`CMD` 維持 bash；容器無 systemd → 文件註明 `serialwrap setup` 自動退 on-demand。

- [ ] **Step 3: `README.md` 安裝段重寫**為：

```bash
pipx install "git+https://github.com/hamanpaul/serialwrap@v0.1.0"
serialwrap setup
serialwrap doctor
```

附 `sudo usermod -aG dialout $USER`、WSL `/etc/wsl.conf [boot] systemd=true` + `wsl --shutdown`、on-demand 降級說明；刪掉「預設 /usr/local/bin」與實際 `~/.paul_tools` 不符的舊句。

- [ ] **Step 4: 驗證** — `bash -n install.sh`、`docker build -t sw-smoke .`（若有 docker）、README 連結/marker 檢查。

- [ ] **Step 5: Commit**

```bash
git add install.sh Dockerfile README.md
git commit -m "docs(install): install.sh/Dockerfile/README 對齊 pipx+setup 流程"
```

### Task 14: CI 打包 smoke + 收尾

**Files:**
- Create: `.github/workflows/package.yml`
- Modify: `CHANGELOG.md`、`VERSION`（若升版）

- [ ] **Step 1: 新增 `.github/workflows/package.yml`** — build wheel → 乾淨 venv `pip install dist/*.whl` → 斷言 `serialwrap --help`、`serialwrapd --help`、`serialwrap doctor` 可跑。

- [ ] **Step 2: `CHANGELOG.md [Unreleased]`** 新增 install-flow 條目（pipx+systemd、setup/doctor、XDG、向後相容遷移）。

- [ ] **Step 3: 全套件測試 + policy（含 PR 上下文）**

Run:
```bash
python3 -m pytest -q tests/
python3 -m policy_check --repo . --pr-title "feat(install): pipx+systemd 安裝流程" \
  --pr-body "$(cat /tmp/pr_body.md)" --pr-base-ref main --pr-head-ref feature/install-flow-systemd-pipx
```
Expected: 無新失敗；policy `fail: 0`

- [ ] **Step 4: 真機驗證**（手動，沿用既有方法論，以 env 覆寫隔離）：
  1. 全新 `pipx install @<sha>` + `serialwrap setup`（systemd 啟用）→ `systemctl --user status serialwrap` active、認線、`cmd submit` 通。
  2. 無 systemd（on-demand）→ 啟用 WSL systemd → 重跑 `setup` → 乾淨轉 systemd-user、單一 daemon（無 two-reader）、state 保留。

- [ ] **Step 5: Commit + PR**

```bash
git add .github/workflows/package.yml CHANGELOG.md
git commit -m "ci(package): 打包 smoke workflow + CHANGELOG"
```

---

## Self-Review（作者自查）

- **Spec coverage**：packaging-distribution→Tasks 1–3；runtime-paths→Tasks 4–5；daemon-supervision→Tasks 6–9；install-setup→Tasks 10–12；文件/相容/CI→Tasks 13–14。四個 capability 的 ADDED requirements 皆有對應任務與測試。
- **Placeholder 掃描**：每個 code step 均含實際程式/指令；無 TODO/TBD。
- **型別/命名一致**：`should_auto_spawn`、`RuntimeConfig.mode()/socket_path()/set_mode()`、`Effects.run/has_systemd/user_in_group`、`render_user_unit/render_system_unit`、`service_action`、`materialize_assets/reconcile/FlashingBusy`、`run_doctor`、`migrate_legacy_state`、`sw_core.assets.copy_tree/list_*` 跨任務一致。
