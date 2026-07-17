"""tests/test_daemon_service_selector.py — Task 10 daemon/service 接線 selector 驗證。

驗證：
- daemon.build_parser() 的 --socket 預設為 DEFAULT_ENDPOINT（#84 PORT-4）
- _build_lock()：Windows 後端回 WindowsSingletonLock；POSIX 後端回 SingletonLock（跳過，fcntl 不適用）
- _build_server()：Windows 後端回 TcpRpcServer；POSIX 後端回 JsonRpcUnixServer
- _load_exclude_coms()：從 config.yaml 的 windows.exclude_coms 讀取排除清單
- RuntimeConfig.set_socket()：只更新 socket_path，不改動 supervision_mode
- DeviceWatcher 在 Windows 後端取到 WindowsDeviceSource（monkeypatch）
"""
from __future__ import annotations

import os
import sys

import pytest

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


@pytest.fixture(autouse=True)
def _iso_state():
    """#120 per-file 隔離：SerialwrapService([]) 建構會落 state.json（單檔直跑防線）。"""
    with state_iso.isolated_state():
        yield


# ─────────────────────────── build_parser ──────────────────────────────────


def test_daemon_parser_default_socket_equals_default_endpoint():
    """--socket 預設應為 DEFAULT_ENDPOINT（POSIX 等同 SOCKET_PATH，Windows 為 TCP URL）（#84 PORT-4）。"""
    from sw_core.constants import DEFAULT_ENDPOINT
    from sw_core.daemon import build_parser

    args = build_parser().parse_args([])
    assert args.socket == DEFAULT_ENDPOINT


# ─────────────────────────── _build_lock ───────────────────────────────────


def test_build_lock_win_returns_windows_singleton_lock(monkeypatch, tmp_path):
    """win 後端：_build_lock 應回 WindowsSingletonLock。"""
    import sw_core.platform_backends as pb
    monkeypatch.setattr(pb, "select_lock_backend", lambda backend=None: "win")

    from sw_core.daemon import _build_lock, build_parser

    args = build_parser().parse_args(
        ["--lock", str(tmp_path / "test.lock"), "--socket", "tcp://127.0.0.1:48700"]
    )
    lock = _build_lock(args)
    from sw_core.lock_win import WindowsSingletonLock
    assert isinstance(lock, WindowsSingletonLock)
    assert lock.lock_path == str(tmp_path / "test.lock")
    assert lock.endpoint == "tcp://127.0.0.1:48700"


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only: lock_posix 使用 fcntl，不適用於 Windows（#84 PORT-4）",
)
def test_build_lock_posix_returns_singleton_lock(monkeypatch, tmp_path):
    """posix 後端：_build_lock 應回 SingletonLock（POSIX 限定）。"""
    import sw_core.platform_backends as pb
    monkeypatch.setattr(pb, "select_lock_backend", lambda backend=None: "posix")

    from sw_core.daemon import _build_lock, build_parser
    from sw_core.lock_posix import SingletonLock

    args = build_parser().parse_args(
        ["--lock", str(tmp_path / "test.lock"), "--socket", str(tmp_path / "test.sock")]
    )
    lock = _build_lock(args)
    assert isinstance(lock, SingletonLock)


# ─────────────────────────── _build_server ─────────────────────────────────


def test_build_server_win_returns_tcp_rpc_server(monkeypatch):
    """win 後端：_build_server 應回 TcpRpcServer。"""
    import sw_core.platform_backends as pb
    monkeypatch.setattr(pb, "select_rpc_backend", lambda backend=None: "win")

    from sw_core.daemon import _build_server, build_parser
    from sw_core.rpc_win import TcpRpcServer

    args = build_parser().parse_args(["--socket", "tcp://127.0.0.1:48700"])
    server = _build_server(args, lambda m, p: {"ok": True})
    assert isinstance(server, TcpRpcServer)


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only: JsonRpcUnixServer 使用 AF_UNIX，不適用於 Windows（#84 PORT-4）",
)
def test_build_server_posix_returns_unix_server(monkeypatch, tmp_path):
    """posix 後端：_build_server 應回 JsonRpcUnixServer（POSIX 限定）。"""
    import sw_core.platform_backends as pb
    monkeypatch.setattr(pb, "select_rpc_backend", lambda backend=None: "posix")

    from sw_core.daemon import _build_server, build_parser
    from sw_core.rpc_posix import JsonRpcUnixServer

    args = build_parser().parse_args(["--socket", str(tmp_path / "test.sock")])
    server = _build_server(args, lambda m, p: {"ok": True})
    assert isinstance(server, JsonRpcUnixServer)


# ──────────────────────── _write_config_endpoint ───────────────────────────


def test_write_config_endpoint_writes_socket_path(tmp_path, monkeypatch):
    """_write_config_endpoint 應寫入 socket_path 到 config.yaml，並保留 supervision_mode。"""
    from sw_core.runtime_config import RuntimeConfig
    import sw_core.daemon as daemon_mod

    config_path = tmp_path / "config.yaml"
    # 先寫入一個有 supervision_mode 的 config
    RuntimeConfig(config_path).set_mode("on-demand")

    monkeypatch.setattr(daemon_mod, "CONFIG_DIR", str(tmp_path))
    daemon_mod._write_config_endpoint("tcp://127.0.0.1:48700")

    rc = RuntimeConfig(config_path)
    assert rc.socket_path() == "tcp://127.0.0.1:48700"
    assert rc.mode() == "on-demand"  # supervision_mode 不應被覆蓋


# ─────────────────────── RuntimeConfig.set_socket ──────────────────────────


def test_set_socket_only_updates_socket_path(tmp_path):
    """set_socket 只改 socket_path，不動 supervision_mode。"""
    from sw_core.runtime_config import RuntimeConfig

    rc = RuntimeConfig(tmp_path / "config.yaml")
    rc.set_mode("systemd-user", socket_path="/run/old.sock")

    rc2 = RuntimeConfig(tmp_path / "config.yaml")
    rc2.set_socket("tcp://127.0.0.1:48700")

    rc3 = RuntimeConfig(tmp_path / "config.yaml")
    assert rc3.socket_path() == "tcp://127.0.0.1:48700"
    assert rc3.mode() == "systemd-user"  # supervision_mode 維持不變


def test_set_socket_creates_config_if_absent(tmp_path):
    """config.yaml 不存在時，set_socket 仍可建立並寫入。"""
    from sw_core.runtime_config import RuntimeConfig

    config_path = tmp_path / "sub" / "config.yaml"
    rc = RuntimeConfig(config_path)
    rc.set_socket("tcp://127.0.0.1:48700")

    rc2 = RuntimeConfig(config_path)
    assert rc2.socket_path() == "tcp://127.0.0.1:48700"
    assert rc2.mode() is None  # supervision_mode 未設定


# ──────────────────────── _load_exclude_coms ───────────────────────────────


def test_load_exclude_coms_reads_windows_config(tmp_path, monkeypatch):
    """_load_exclude_coms 應從 config.yaml 的 windows.exclude_coms 讀取排除清單。

    #131：實作移居 device_source（doctor 免拖 daemon stack），service 保留匯入相容，
    故 CONFIG_DIR 於 device_source 模組層 patch。
    """
    import sw_core.device_source as ds_mod
    import sw_core.service as svc_mod
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"windows": {"exclude_coms": ["COM1", "COM3"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ds_mod, "CONFIG_DIR", str(tmp_path))

    result = svc_mod._load_exclude_coms()  # service 端 alias 仍可用
    assert result == {"COM1", "COM3"}


def test_load_exclude_coms_returns_empty_when_absent(tmp_path, monkeypatch):
    """config.yaml 不存在或無 windows.exclude_coms 時，回傳空集合。"""
    import sw_core.device_source as ds_mod
    import sw_core.service as svc_mod

    monkeypatch.setattr(ds_mod, "CONFIG_DIR", str(tmp_path))

    result = svc_mod._load_exclude_coms()
    assert result == set()


def test_load_exclude_coms_empty_when_no_windows_section(tmp_path, monkeypatch):
    """config.yaml 有 supervision_mode 但無 windows 段時，回傳空集合。"""
    import sw_core.device_source as ds_mod
    import sw_core.service as svc_mod
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"supervision_mode": "on-demand"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ds_mod, "CONFIG_DIR", str(tmp_path))

    result = svc_mod._load_exclude_coms()
    assert result == set()


# ──────────────────── DeviceWatcher source 選擇（service.py） ──────────────


def test_service_uses_windows_device_source_when_win_backend(monkeypatch, tmp_path):
    """Windows 後端：SerialwrapService 的 DeviceWatcher 應接到 WindowsDeviceSource。"""
    import sw_core.platform_backends as pb
    import sw_core.service as svc_mod
    from sw_core.device_source import WindowsDeviceSource

    monkeypatch.setattr(pb, "select_device_backend", lambda backend=None: "win")
    monkeypatch.setattr(svc_mod, "CONFIG_DIR", str(tmp_path))

    svc = svc_mod.SerialwrapService([])
    # DeviceWatcher._source 應為 WindowsDeviceSource
    assert isinstance(svc._watcher._source, WindowsDeviceSource)


def test_service_uses_posix_device_source_when_posix_backend(monkeypatch, tmp_path):
    """POSIX 後端：SerialwrapService 的 DeviceWatcher 應接到 PosixDeviceSource（預設）。"""
    import sw_core.platform_backends as pb
    import sw_core.service as svc_mod
    from sw_core.device_source import PosixDeviceSource

    monkeypatch.setattr(pb, "select_device_backend", lambda backend=None: "posix")
    monkeypatch.setattr(svc_mod, "CONFIG_DIR", str(tmp_path))

    svc = svc_mod.SerialwrapService([])
    assert isinstance(svc._watcher._source, PosixDeviceSource)
