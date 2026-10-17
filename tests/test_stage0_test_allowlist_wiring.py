"""tests/test_stage0_test_allowlist_wiring.py — Task P1-T13：OD-11／SC-19 接線與 fail-closed 補充測試。

鎖定測試 ``tests/test_stage0_test_allowlist.py`` 只覆蓋純函式與直接建構的 ``SerialwrapService``，
無法偵測下列退化：service 未包住裝置來源、daemon 未傳遞判定、``config.yaml`` 未被讀取、
「等於預設」判定失效、前綴比對、config 無法解析時隔離實例退回全域掃描、錯誤碼字面值與常數漂移。
本檔補上這些測試；不修改鎖定測試的斷言。

依 P0 規則：不在模組頂層 import 本 Phase 新增的名稱，一律於測試函式內以 ``_load()`` 延遲匯入；
每個測試函式恰標一個平台 marker（``core_portable``／``posix_only``）與至少一個資源 marker。
"""
from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ENV_ALLOWLIST = "SERIALWRAP_TEST_DEVICE_ALLOWLIST"
ISO_VARS = ("SERIALWRAP_STATE_DIR", "SERIALWRAP_RUN_DIR", "SERIALWRAP_CONFIG_DIR")
ISO_SUBDIRS = ("state", "run", "cfg")

# 無法讀取或解析的 config.yaml，與 PyYAML 拋出的例外類別名（daemon 只寫類別名到拒絕訊息）。
BAD_CONFIGS: dict[str, tuple[str, str]] = {
    "invalid_date": ("built: 2023-02-30\n", "ValueError"),
    "tab_indent": ("a:\n\tb: 1\n", "ScannerError"),
    "nul_bytes": ("\x00\x01", "ReaderError"),
    "deep_nesting": ("a: " + "[" * 5000 + "]" * 5000 + "\n", "RecursionError"),
}


def _load(*names: str) -> tuple[Any, ...]:
    """延遲匯入 sw_core.test_allowlist 並取出指定名稱。"""
    mod = importlib.import_module("sw_core.test_allowlist")
    return tuple(getattr(mod, name) for name in names)


def _iso_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    for var, sub in zip(ISO_VARS, ISO_SUBDIRS):
        monkeypatch.setenv(var, str(root / "iso" / sub))


def _write_config(cfg_dir: Path, text: str) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(text, encoding="utf-8")


# ---- 接線：service 裝置來源只經 filter_allowlist 過濾 ----


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_service_watcher_discovers_only_allowlisted_devices_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core.service import SerialwrapService
    from sw_core.test_allowlist import AllowlistDecision
    from sw_core.wal import WalWriter

    by_id = tmp_path / "by-id"
    by_path = tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    (tmp_path / "ttyA").write_text("")
    (tmp_path / "ttyB").write_text("")
    os.symlink(tmp_path / "ttyA", by_id / "usb-A")
    os.symlink(tmp_path / "ttyB", by_id / "usb-B")
    monkeypatch.setattr("sw_core.service.WalWriter", lambda *a, **kw: WalWriter(wal_dir=str(tmp_path / "wal")))

    control = SerialwrapService([], templates=[], by_id_dir=str(by_id), by_path_dir=str(by_path))
    control._watcher.poll_once()
    assert sorted(control._watcher.devices) == sorted([str(by_id / "usb-A"), str(by_id / "usb-B")])

    allowed = str(by_id / "usb-A")
    svc = SerialwrapService(
        [], templates=[], by_id_dir=str(by_id), by_path_dir=str(by_path),
        test_allowlist=AllowlistDecision(True, (allowed,), "env", None),
    )
    svc._watcher.poll_once()
    assert list(svc._watcher.devices) == [allowed]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_service_watcher_discovers_only_allowlisted_devices_win_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core import device_source
    from sw_core.service import SerialwrapService
    from sw_core.test_allowlist import AllowlistDecision
    from sw_core.wal import WalWriter

    monkeypatch.setenv("SERIALWRAP_DEVICE_BACKEND", "win")
    monkeypatch.setattr(device_source, "_read_serialcomm", lambda: {r"\Device\VCP0": "COM7", r"\Device\VCP1": "COM8"})
    monkeypatch.setattr(device_source, "_read_bt_ports", lambda: set())
    monkeypatch.setattr("sw_core.service._load_exclude_coms", lambda: set())
    monkeypatch.setattr("sw_core.service.WalWriter", lambda *a, **kw: WalWriter(wal_dir=str(tmp_path / "wal")))

    control = SerialwrapService([], templates=[])
    control._watcher.poll_once()
    assert sorted(control._watcher.devices) == ["COM7", "COM8"]

    svc = SerialwrapService([], templates=[], test_allowlist=AllowlistDecision(True, ("COM7",), "env", None))
    svc._watcher.poll_once()
    assert list(svc._watcher.devices) == ["COM7"]


# ---- 接線：daemon 讀取判定並傳入 service；config.yaml 被讀取 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_passes_enabled_decision_to_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core import daemon
    from sw_core.test_allowlist import AllowlistDecision

    captured: dict[str, Any] = {}

    class _Lock:
        def acquire(self) -> None: ...
        def release(self) -> None: ...

    def _fake_service(*_a: Any, **kw: Any) -> None:
        captured.update(kw)
        raise daemon.StateLoadError("stop-after-capture")

    _iso_env(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_ALLOWLIST, "COM7")
    monkeypatch.setattr(daemon, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(daemon, "load_profiles", lambda _d: SimpleNamespace(profiles=[], templates=[], max_sessions=16))
    monkeypatch.setattr(daemon, "_build_lock", lambda _a: _Lock())
    monkeypatch.setattr(daemon, "SerialwrapService", _fake_service)
    rc = asyncio.run(daemon._run_async(daemon.build_parser().parse_args([])))
    assert rc == 3
    assert captured["test_allowlist"] == AllowlistDecision(True, ("COM7",), "env", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_reads_allowlist_from_config_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core import daemon
    from sw_core.test_allowlist import AllowlistDecision

    cfg = tmp_path / "iso" / "cfg"
    _write_config(cfg, "windows:\n  test_device_allowlist: [COM9]\n")
    _iso_env(monkeypatch, tmp_path)
    monkeypatch.delenv(ENV_ALLOWLIST, raising=False)
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(cfg))
    assert daemon._resolve_test_allowlist() == AllowlistDecision(True, ("COM9",), "config", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_load_config_data_missing_file_means_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core import daemon

    monkeypatch.setattr(daemon, "CONFIG_DIR", str(tmp_path / "nowhere"))
    assert daemon._load_config_data() == (None, None)


# ---- 「等於預設」判定不得 fail open ----


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("var,xdg", [
    ("SERIALWRAP_STATE_DIR", "XDG_STATE_HOME"),
    ("SERIALWRAP_RUN_DIR", "XDG_RUNTIME_DIR"),
    ("SERIALWRAP_CONFIG_DIR", "XDG_CONFIG_HOME"),
])
def test_daemon_rejects_dir_equal_to_real_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str, xdg: str):
    from sw_core import daemon

    for name in ("XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    _iso_env(monkeypatch, tmp_path)
    monkeypatch.setenv(var, str(tmp_path / xdg.lower() / "serialwrap"))
    monkeypatch.setenv(ENV_ALLOWLIST, "COM7")
    with pytest.raises(RuntimeError) as ei:
        daemon._resolve_test_allowlist()
    assert ei.value.args == ("TEST_DEVICE_ALLOWLIST_REJECTED", f"not an isolated instance: {var} equals default")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_missing_default_baseline_fails_closed():
    (resolve_test_allowlist,) = _load("resolve_test_allowlist")
    env = {
        "SERIALWRAP_STATE_DIR": "/tmp/iso/state",
        "SERIALWRAP_RUN_DIR": "/tmp/iso/run",
        "SERIALWRAP_CONFIG_DIR": "/tmp/iso/cfg",
        ENV_ALLOWLIST: "COM7",
    }
    d = resolve_test_allowlist(env, None, defaults={})
    assert d.enabled is False
    assert d.rejected_reason == "not an isolated instance: SERIALWRAP_STATE_DIR default unknown"


# ---- 精確比對 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_filter_is_exact_not_prefix():
    from sw_core.device_watcher import DeviceInfo

    (filter_allowlist,) = _load("filter_allowlist")
    devs = {
        "COM1": DeviceInfo(by_id="COM1", real_path=r"\\.\COM1"),
        "COM10": DeviceInfo(by_id="COM10", real_path=r"\\.\COM10"),
    }
    assert list(filter_allowlist(devs, ("COM1",))) == ["COM1"]


# ---- config.yaml 無法讀取或解析：隔離實例 fail closed，非隔離實例維持未設定 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("name", list(BAD_CONFIGS))
def test_isolated_instance_fails_closed_on_unreadable_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str):
    from sw_core import daemon

    text, expected = BAD_CONFIGS[name]
    _iso_env(monkeypatch, tmp_path)
    monkeypatch.delenv(ENV_ALLOWLIST, raising=False)
    cfg = tmp_path / "iso" / "cfg"
    _write_config(cfg, text)
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(cfg))
    with pytest.raises(RuntimeError) as ei:
        daemon._resolve_test_allowlist()
    assert ei.value.args == ("TEST_DEVICE_ALLOWLIST_REJECTED", f"invalid config.yaml: {expected}")


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("name", list(BAD_CONFIGS))
def test_non_isolated_instance_tolerates_unreadable_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str):
    from sw_core import daemon
    from sw_core.test_allowlist import AllowlistDecision

    text, _ = BAD_CONFIGS[name]
    for var in ISO_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(ENV_ALLOWLIST, raising=False)
    cfg = tmp_path / "cfg"
    _write_config(cfg, text)
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(cfg))
    assert daemon._resolve_test_allowlist() == AllowlistDecision(False, (), "none", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_env_allowlist_overrides_unreadable_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from sw_core import daemon
    from sw_core.test_allowlist import AllowlistDecision

    _iso_env(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_ALLOWLIST, "COM7")
    cfg = tmp_path / "iso" / "cfg"
    _write_config(cfg, BAD_CONFIGS["tab_indent"][0])
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(cfg))
    assert daemon._resolve_test_allowlist() == AllowlistDecision(True, ("COM7",), "env", None)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_run_async_rejects_isolated_unreadable_config_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from sw_core import daemon

    ensure_calls: list[bool] = []
    lock_calls: list[bool] = []
    service_calls: list[bool] = []

    cfg = tmp_path / "iso" / "cfg"
    _write_config(cfg, BAD_CONFIGS["tab_indent"][0])
    _iso_env(monkeypatch, tmp_path)
    monkeypatch.delenv(ENV_ALLOWLIST, raising=False)
    monkeypatch.setattr(daemon, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(daemon, "ensure_runtime_dirs", lambda: ensure_calls.append(True))
    monkeypatch.setattr(daemon, "_build_lock", lambda args: lock_calls.append(True))
    monkeypatch.setattr(daemon, "SerialwrapService", lambda *a, **kw: service_calls.append(True))

    ret = asyncio.run(daemon._run_async(daemon.build_parser().parse_args([])))
    assert ret == 2
    assert ensure_calls == [] and lock_calls == [] and service_calls == []
    assert capsys.readouterr().err == "serialwrapd: TEST_DEVICE_ALLOWLIST_REJECTED: invalid config.yaml: ScannerError\n"


# ---- 錯誤碼：字面值（extractor sink 所需）須與模組常數一致 ----


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_error_code_literal_matches_constant():
    enforce_allowlist, allowlist_decision, error_code = _load(
        "enforce_allowlist", "AllowlistDecision", "ERROR_TEST_DEVICE_ALLOWLIST_REJECTED",
    )
    assert error_code == "TEST_DEVICE_ALLOWLIST_REJECTED"
    with pytest.raises(RuntimeError) as ei:
        enforce_allowlist(allowlist_decision(False, (), "env", "not an isolated instance: SERIALWRAP_STATE_DIR unset"))
    assert ei.value.args[0] == error_code
