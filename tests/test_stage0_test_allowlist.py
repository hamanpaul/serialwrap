"""tests/test_stage0_test_allowlist.py — Task P1-T13：OD-11／SC-19 隔離實例測試裝置 allowlist 失敗測試（TDD 紅燈）。

依據：docs/rust-refactor/plans/02-stage0-python-bc.md §Task T13 原文。
規格：
- 僅對隔離實例生效，正式 daemon 設定遇到即拒絕；
- 只接受明確列出的名稱；不關閉獨占鎖、不更換 serial backend；
- 啟用時 daemon status 必須顯示；
- 依 02 Global Constraints：本模組不在頂層 import 本 Phase 新增的 `sw_core.test_allowlist`，
  一律於測試函式內以 `_load()` 延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sw_core.device_watcher import DeviceInfo

DEF = {
    "SERIALWRAP_STATE_DIR": "$HOME/.local/state/serialwrap",
    "SERIALWRAP_RUN_DIR": "/run/user/1000/serialwrap",
    "SERIALWRAP_CONFIG_DIR": "$HOME/.config/serialwrap",
}
ISO = {
    "SERIALWRAP_STATE_DIR": "/tmp/iso/state",
    "SERIALWRAP_RUN_DIR": "/tmp/iso/run",
    "SERIALWRAP_CONFIG_DIR": "/tmp/iso/cfg",
}


def _load(*names: str) -> tuple[Any, ...]:
    """延遲匯入 sw_core.test_allowlist 並取出指定名稱。"""
    mod = importlib.import_module("sw_core.test_allowlist")
    return tuple(getattr(mod, name) for name in names)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_disabled_when_unset():
    resolve_test_allowlist, AllowlistDecision = _load("resolve_test_allowlist", "AllowlistDecision")
    assert resolve_test_allowlist({}, None, defaults=DEF) == AllowlistDecision(False, (), "none", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_env_takes_precedence_and_dedups():
    resolve_test_allowlist, AllowlistDecision = _load("resolve_test_allowlist", "AllowlistDecision")
    d = resolve_test_allowlist(
        {**ISO, "SERIALWRAP_TEST_DEVICE_ALLOWLIST": " COM7, COM8,COM7,"},
        {"windows": {"test_device_allowlist": ["COM9"]}},
        defaults=DEF,
    )
    assert d == AllowlistDecision(True, ("COM7", "COM8"), "env", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_config_used_when_env_absent():
    (resolve_test_allowlist,) = _load("resolve_test_allowlist")
    d = resolve_test_allowlist(ISO, {"windows": {"test_device_allowlist": ["COM9"]}}, defaults=DEF)
    assert d.coms == ("COM9",) and d.source == "config" and d.rejected_reason is None


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("drop,reason", [
    ("SERIALWRAP_STATE_DIR", "not an isolated instance: SERIALWRAP_STATE_DIR unset"),
    ("SERIALWRAP_RUN_DIR", "not an isolated instance: SERIALWRAP_RUN_DIR unset"),
    ("SERIALWRAP_CONFIG_DIR", "not an isolated instance: SERIALWRAP_CONFIG_DIR unset"),
])
def test_rejected_when_not_isolated(drop: str, reason: str):
    (resolve_test_allowlist,) = _load("resolve_test_allowlist")
    env = {k: v for k, v in ISO.items() if k != drop}
    env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = "COM7"
    assert resolve_test_allowlist(env, None, defaults=DEF).rejected_reason == reason


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rejected_when_dir_equals_default():
    (resolve_test_allowlist,) = _load("resolve_test_allowlist")
    env = {**ISO, "SERIALWRAP_STATE_DIR": DEF["SERIALWRAP_STATE_DIR"], "SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM7"}
    assert resolve_test_allowlist(env, None, defaults=DEF).rejected_reason == \
        "not an isolated instance: SERIALWRAP_STATE_DIR equals default"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_filter_keeps_only_listed_exact_names():
    (filter_allowlist,) = _load("filter_allowlist")
    devs = {
        "COM7": DeviceInfo(by_id="COM7", real_path=r"\\.\COM7"),
        "COM8": DeviceInfo(by_id="COM8", real_path=r"\\.\COM8"),
    }
    assert list(filter_allowlist(devs, ("COM7",))) == ["COM7"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_invalid_config_type_rejected():
    (resolve_test_allowlist,) = _load("resolve_test_allowlist")
    d1 = resolve_test_allowlist(ISO, {"windows": {"test_device_allowlist": "COM7"}}, defaults=DEF)
    assert d1.rejected_reason == "invalid windows.test_device_allowlist"
    d2 = resolve_test_allowlist(ISO, {"windows": {"test_device_allowlist": [123]}}, defaults=DEF)
    assert d2.rejected_reason == "invalid windows.test_device_allowlist"


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_daemon_refuses_start_when_rejected(tmp_path: Path):
    _load("resolve_test_allowlist")
    env = os.environ.copy()
    for k in list(env.keys()):
        if k.startswith("SERIALWRAP_"):
            env.pop(k, None)
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(fake_home)
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "run")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["LOCALAPPDATA"] = str(tmp_path / "localappdata")
    env["APPDATA"] = str(tmp_path / "appdata")
    env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] = "COM7"

    proc = subprocess.run(
        [sys.executable, "-m", "sw_core.daemon"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert proc.stderr == "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: not an isolated instance: SERIALWRAP_STATE_DIR unset\n"
    created_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created_files == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reject_runs_before_runtime_dirs_and_lock(monkeypatch: pytest.MonkeyPatch):
    _load("resolve_test_allowlist")
    from sw_core import daemon

    ensure_calls: list[bool] = []
    lock_calls: list[bool] = []
    service_calls: list[bool] = []

    monkeypatch.setattr(daemon, "ensure_runtime_dirs", lambda: ensure_calls.append(True))
    monkeypatch.setattr(daemon, "_build_lock", lambda args: lock_calls.append(True))
    monkeypatch.setattr(daemon, "SerialwrapService", lambda *a, **kw: service_calls.append(True))
    monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", "COM7")
    monkeypatch.delenv("SERIALWRAP_STATE_DIR", raising=False)

    parser = daemon.build_parser()
    args = parser.parse_args([])

    ret = asyncio.run(daemon._run_async(args))
    assert ret == 2
    assert len(ensure_calls) == 0
    assert len(lock_calls) == 0
    assert len(service_calls) == 0


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_rejection_code_reaches_extraction_sink():
    _load("ERROR_TEST_DEVICE_ALLOWLIST_REJECTED")
    proc = subprocess.run(
        [sys.executable, "-m", "tools.contracts", "extract", "--namespace", "error_code", "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    emissions = json.loads(proc.stdout)
    matching = [
        e for e in emissions
        if e.get("value") == "TEST_DEVICE_ALLOWLIST_REJECTED" and e.get("implies_interface") == "proc"
    ]
    assert len(matching) >= 1


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_health_status_shows_allowlist_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (AllowlistDecision,) = _load("AllowlistDecision")
    from sw_core.service import SerialwrapService
    from sw_core.wal import WalWriter

    decision = AllowlistDecision(enabled=True, coms=("COM7",), source="env", rejected_reason=None)
    monkeypatch.setattr("sw_core.service.WalWriter", lambda *a, **kw: WalWriter(wal_dir=str(tmp_path / "wal")))
    service = SerialwrapService([], templates=[], test_allowlist=decision)
    h = service.health()
    assert "test_device_allowlist" in h
    assert h["test_device_allowlist"] == {
        "enabled": True,
        "coms": ["COM7"],
        "source": "env",
    }
    keys = list(h.keys())
    assert "multi_open_detail" in keys
    assert keys.index("test_device_allowlist") > keys.index("multi_open_detail")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_health_status_omits_key_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _load("AllowlistDecision")
    from sw_core.service import SerialwrapService
    from sw_core.wal import WalWriter

    monkeypatch.setattr("sw_core.service.WalWriter", lambda *a, **kw: WalWriter(wal_dir=str(tmp_path / "wal")))
    service = SerialwrapService([], templates=[])
    h = service.health()
    assert "test_device_allowlist" not in h


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_allowlist_does_not_change_serial_backend_or_lock(monkeypatch: pytest.MonkeyPatch):
    _load("resolve_test_allowlist")
    import fcntl
    import pty
    from sw_core.config import UartProfile
    from sw_core import serial_port as sp

    monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", "COM7")

    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    try:
        profile = UartProfile()
        port = sp.open_serial_port(slave_path, profile)
        assert isinstance(port, sp._PosixSerialPort)

        flock_calls: list[int] = []
        real_flock = fcntl.flock
        monkeypatch.setattr(sp.fcntl, "flock", lambda fd, op: (flock_calls.append(op), real_flock(fd, op))[1])

        port.open()
        try:
            assert len(flock_calls) >= 1
        finally:
            port.close()
    finally:
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_posix_by_id_allowlist_filters_symlink_paths():
    (filter_allowlist,) = _load("filter_allowlist")
    devs = {
        "/dev/serial/by-id/usb-FTDI_1": DeviceInfo(by_id="/dev/serial/by-id/usb-FTDI_1", real_path="/dev/ttyUSB0"),
        "/dev/serial/by-id/usb-FTDI_2": DeviceInfo(by_id="/dev/serial/by-id/usb-FTDI_2", real_path="/dev/ttyUSB1"),
    }
    filtered = filter_allowlist(devs, ("/dev/serial/by-id/usb-FTDI_1",))
    assert list(filtered.keys()) == ["/dev/serial/by-id/usb-FTDI_1"]
    assert filtered["/dev/serial/by-id/usb-FTDI_1"].real_path == "/dev/ttyUSB0"
