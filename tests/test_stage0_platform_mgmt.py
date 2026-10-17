# tests/test_stage0_platform_mgmt.py
"""Task P1-T15 平台管理（SC-08、SC-09、SC-10、SC-12、SC-20、DF-14）測試。

依 02-stage0-python-bc.md Global Constraints：
本模組不在頂層 import 本 Task 或本 Phase 新增的未實作函式／類別，
一律於測試函式內延遲 import 或透過 `_load()` 輔助函式取出，
確保 pytest collection 階段完全正常。

marker 規則（P0 T14）：
每個測試項目依其平台與資源特性標註恰好一個平台 marker 與至少一個資源 marker；
本檔混合平台與資源，故不在模組層設置 pytestmark，一律於各測試函式層顯式標註。
"""
from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest


def _load(module_name: str, *names: str) -> list[Any]:
    """延遲 import 模組並取出名稱；相依模組或名稱尚未實作時於測試函式內拋出 ImportError。"""
    mod = importlib.import_module(module_name)
    out: list[Any] = []
    for name in names:
        if not hasattr(mod, name):
            raise ImportError(f"cannot import name {name!r} from {module_name!r}")
        out.append(getattr(mod, name))
    return out


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_config_env_wins(tmp_path):
    from sw_core import constants

    p = tmp_path / "explicit.yaml"
    assert constants.resolve_config_path(env={"SERIALWRAP_CONFIG": str(p)}) == str(p)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_user_config_then_system_fallback(tmp_path, monkeypatch):
    from sw_core import constants

    monkeypatch.setattr(constants, "CONFIG_DIR", str(tmp_path / "cfg"))
    sysf = tmp_path / "etc" / "config.yaml"
    sysf.parent.mkdir(parents=True, exist_ok=True)
    sysf.write_text("supervision_mode: on-demand\n", encoding="utf-8")
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(sysf), raising=False)
    assert constants.resolve_config_path(env={}) == str(sysf)
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cfg" / "config.yaml").write_text("", encoding="utf-8")
    assert constants.resolve_config_path(env={}) == str(tmp_path / "cfg" / "config.yaml")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_runtime_config_reads_system_fallback(tmp_path, monkeypatch):
    from sw_core import cli, constants

    user_dir = tmp_path / "user-config"
    system_path = tmp_path / "system" / "config.yaml"
    system_path.parent.mkdir(parents=True)
    system_path.write_text(
        "supervision_mode: systemd-user\nsocket_path: /run/systemd.sock\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(constants, "CONFIG_DIR", str(user_dir))
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(system_path))
    monkeypatch.delenv("SERIALWRAP_CONFIG", raising=False)
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(user_dir))

    config = cli._default_runtime_config()

    assert config.mode() == "systemd-user"
    assert config.socket_path() == "/run/systemd.sock"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_reads_system_fallback_and_writes_user_config(tmp_path, monkeypatch):
    from sw_core import constants
    from sw_core.setup_cmd import reconcile
    from sw_core.sysenv import FakeEffects
    import yaml

    user_dir = tmp_path / "user-config"
    system_path = tmp_path / "system" / "config.yaml"
    system_path.parent.mkdir(parents=True)
    system_path.write_text(
        "supervision_mode: on-demand\nsocket_path: /run/old.sock\n",
        encoding="utf-8",
    )
    original = system_path.read_text(encoding="utf-8")
    monkeypatch.setattr(constants, "CONFIG_DIR", str(user_dir))
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(system_path))
    monkeypatch.delenv("SERIALWRAP_CONFIG", raising=False)
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(user_dir))

    reconcile(
        old_mode="on-demand",
        target_mode="on-demand",
        fx=FakeEffects(),
        home=tmp_path,
        socket_path="/run/new.sock",
    )

    written = yaml.safe_load((user_dir / "config.yaml").read_text(encoding="utf-8"))
    assert written == {"supervision_mode": "on-demand", "socket_path": "/run/new.sock"}
    assert system_path.read_text(encoding="utf-8") == original


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_device_source_reads_system_exclude_coms(tmp_path, monkeypatch):
    from sw_core import device_source
    from sw_core import constants

    user_dir = tmp_path / "user-config"
    system_path = tmp_path / "system" / "config.yaml"
    system_path.parent.mkdir(parents=True)
    system_path.write_text("windows:\n  exclude_coms: [COM9]\n", encoding="utf-8")
    monkeypatch.setattr(device_source, "CONFIG_DIR", str(user_dir))
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(system_path))
    monkeypatch.delenv("SERIALWRAP_CONFIG", raising=False)

    assert device_source._load_exclude_coms() == {"COM9"}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_config_reader_honors_serialwrap_config(tmp_path, monkeypatch):
    from sw_core import daemon

    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("windows:\n  exclude_coms: [COM7]\n", encoding="utf-8")
    monkeypatch.setenv("SERIALWRAP_CONFIG", str(explicit))

    data, error = daemon._load_config_data()

    assert data == {"windows": {"exclude_coms": ["COM7"]}}
    assert error is None


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_config_reader_reads_system_fallback(tmp_path, monkeypatch):
    from sw_core import constants, daemon

    user_dir = tmp_path / "user-config"
    system_path = tmp_path / "system" / "config.yaml"
    system_path.parent.mkdir(parents=True)
    system_path.write_text("windows:\n  exclude_coms: [COM8]\n", encoding="utf-8")
    monkeypatch.setattr(constants, "CONFIG_DIR", str(user_dir))
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(system_path))
    monkeypatch.delenv("SERIALWRAP_CONFIG", raising=False)

    data, error = daemon._load_config_data()

    assert data == {"windows": {"exclude_coms": ["COM8"]}}
    assert error is None


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_write_never_targets_system_file(tmp_path, monkeypatch):
    from sw_core import constants

    monkeypatch.setattr(constants, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(tmp_path / "etc.yaml"), raising=False)
    (tmp_path / "etc.yaml").write_text("", encoding="utf-8")
    assert constants.resolve_config_path(for_write=True, env={}) == str(tmp_path / "cfg" / "config.yaml")


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("raw,expected_suffix", [
    ("unix:///run/x/../x/s.sock", "/run/x/s.sock"),
    ("/run//x/./s.sock", "/run/x/s.sock"),
    ("tcp://127.0.0.1:48700", "tcp://127.0.0.1:48700"),
])
def test_normalize_endpoint_path(raw, expected_suffix):
    (normalize_endpoint_path,) = _load("sw_core.util", "normalize_endpoint_path")
    assert normalize_endpoint_path(raw).endswith(expected_suffix)


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_socket_default_mode_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("SERIALWRAP_SOCKET_GROUP", raising=False)
    from sw_core.rpc_posix import JsonRpcUnixServer
    import asyncio

    sock = str(tmp_path / "s.sock")
    srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True}, blocking_methods=set())

    async def run():
        await srv.start()
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        await srv.stop()
        return mode

    assert asyncio.run(run()) == 0o600


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_old_daemon_gone_requires_both_conditions():
    (wait_old_daemon_gone,) = _load("sw_core.setup_cmd", "wait_old_daemon_gone")
    t = [0.0]
    seq = iter([True, False, False, False])
    ok = wait_old_daemon_gone(
        "unix:///s",
        42,
        timeout_s=10.0,
        ping=lambda e: False,
        alive=lambda pid: next(seq),
        sleep=lambda s: t.__setitem__(0, t[0] + s),
        monotonic=lambda: t[0],
    )
    assert ok is True
    assert (
        wait_old_daemon_gone(
            "unix:///s",
            42,
            timeout_s=1.0,
            ping=lambda e: True,
            alive=lambda p: False,
            sleep=lambda s: t.__setitem__(0, t[0] + s),
            monotonic=lambda: t[0],
        )
        is False
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_old_daemon_gone_endpoint_down_but_process_alive_times_out():
    (wait_old_daemon_gone,) = _load("sw_core.setup_cmd", "wait_old_daemon_gone")
    t = [0.0]
    assert (
        wait_old_daemon_gone(
            "unix:///s",
            42,
            timeout_s=1.0,
            ping=lambda e: False,
            alive=lambda p: True,
            sleep=lambda s: t.__setitem__(0, t[0] + s),
            monotonic=lambda: t[0],
        )
        is False
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_old_daemon_gone_uses_injected_module_clock(monkeypatch):
    import sw_core.setup_cmd as setup_cmd

    t = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return t[0]

        @staticmethod
        def sleep(seconds):
            t[0] += seconds

    monkeypatch.setattr(setup_cmd, "time", FakeTime)
    assert setup_cmd.wait_old_daemon_gone(
        "/tmp/old.sock",
        42,
        timeout_s=0.4,
        ping=lambda _endpoint: False,
        alive=lambda _pid: True,
    ) is False
    assert t[0] == pytest.approx(0.4)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_old_daemon_gone_unknown_pid_unprovable_times_out():
    (wait_old_daemon_gone,) = _load("sw_core.setup_cmd", "wait_old_daemon_gone")
    t = [0.0]
    assert (
        wait_old_daemon_gone(
            "unix:///s",
            None,
            timeout_s=1.0,
            ping=lambda e: False,
            alive=lambda p: None,
            sleep=lambda s: t.__setitem__(0, t[0] + s),
            monotonic=lambda: t[0],
        )
        is False
    )


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_socket_group_mode_0660(tmp_path, monkeypatch):
    import asyncio
    import grp
    from sw_core.rpc_posix import JsonRpcUnixServer

    group_name = grp.getgrgid(os.getgid()).gr_name
    monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", group_name)
    sock = str(tmp_path / "group.sock")
    srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True}, blocking_methods=set())

    async def run():
        await srv.start()
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        gid = os.stat(sock).st_gid
        await srv.stop()
        return mode, gid

    mode, gid = asyncio.run(run())
    assert mode == 0o660
    assert gid == os.getgid()


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_new_runtime_dirs_0700(tmp_path, monkeypatch):
    from sw_core import constants

    new_run = tmp_path / "new_run"
    existing_cfg = tmp_path / "existing_cfg"
    existing_cfg.mkdir(parents=True, exist_ok=True)
    os.chmod(existing_cfg, 0o751)

    monkeypatch.setattr(constants, "RUN_DIR", str(new_run))
    monkeypatch.setattr(constants, "CONFIG_DIR", str(existing_cfg))
    monkeypatch.setattr(constants, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(constants, "WAL_DIR", str(tmp_path / "wal"))
    monkeypatch.setattr(constants, "EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setattr(constants, "EVENTS_RUNTIME_DIR", str(tmp_path / "events_rt"))

    original_umask = os.umask(0o027)
    try:
        constants.ensure_runtime_dirs()
    finally:
        os.umask(original_umask)

    assert stat.S_IMODE(os.stat(new_run).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(existing_cfg).st_mode) == 0o751


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_lock_compares_normalized_socket(tmp_path):
    import asyncio
    from sw_core.lock_posix import SingletonLock

    target_dir = tmp_path / "a"
    target_dir.mkdir(parents=True, exist_ok=True)
    real_sock = str(target_dir / "s")

    async def fake_handler(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = asyncio.run(asyncio.start_unix_server(fake_handler, path=real_sock))
    try:
        non_normalized_sock = f"unix://{tmp_path}/a/../a/s"
        lock_file = str(tmp_path / "test.lock")
        lk = SingletonLock(lock_file, non_normalized_sock)
        with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
            lk.acquire()
    finally:
        server.close()


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_lock_preserves_symlink_parent_semantics(tmp_path):
    import asyncio
    from sw_core.lock_posix import SingletonLock

    real_parent = tmp_path / "real" / "child"
    real_parent.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real_parent, target_is_directory=True)
    socket_via_link = f"unix://{link}/../s.sock"

    async def fake_handler(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = asyncio.run(
        asyncio.start_unix_server(fake_handler, path=str(tmp_path / "real" / "s.sock"))
    )
    try:
        lock = SingletonLock(str(tmp_path / "lock"), socket_via_link)
        with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
            lock.acquire()
    finally:
        server.close()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_cross_socket_precheck_normalizes(monkeypatch):
    import sw_core.cli as cli

    mock_daemons = [{"pid": 9999, "socket": "/run/x/s.sock"}]
    monkeypatch.setattr("sw_core.multi_open.detect_multi_open", lambda **kw: {"daemons": mock_daemons})

    conflict = cli._find_conflicting_daemon("/run/x/./s.sock")
    assert conflict is None


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows 獨佔檔鎖對測")
@pytest.mark.windows_native
@pytest.mark.memory_only
def test_native_windows_lock_uses_lockfileex(tmp_path, monkeypatch):
    import msvcrt
    from sw_core.lock_win import WindowsSingletonLock

    monkeypatch.setattr("sw_core.lock_win._endpoint_alive", lambda ep: False)
    lock_path = str(tmp_path / "lockfileex.lock")
    lk = WindowsSingletonLock(lock_path, "tcp://127.0.0.1:48796")
    lk.acquire()
    try:
        fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(OSError):
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            os.close(fd)
    finally:
        lk.release()

    fd2 = os.open(lock_path, os.O_RDWR)
    try:
        msvcrt.locking(fd2, msvcrt.LK_NBLCK, 1)
        lk2 = WindowsSingletonLock(lock_path, "tcp://127.0.0.1:48796")
        with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
            lk2.acquire()
    finally:
        msvcrt.locking(fd2, msvcrt.LK_UNLCK, 1)
        os.close(fd2)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_waits_and_refuses_on_timeout(tmp_path, monkeypatch):
    import sw_core.setup_cmd as setup_cmd
    from sw_core.setup_cmd import reconcile
    from sw_core.sysenv import FakeEffects

    t = [0.0]
    class FakeTime:
        @staticmethod
        def time():
            return t[0]
        @staticmethod
        def monotonic():
            return t[0]
        @staticmethod
        def sleep(s):
            t[0] += s

    monkeypatch.setattr(setup_cmd, "time", FakeTime, raising=False)

    fx = FakeEffects(systemd=True)
    res = reconcile(
        old_mode="on-demand",
        target_mode="systemd-user",
        fx=fx,
        home=tmp_path,
        daemon_running=True,
        any_flashing=False,
        old_endpoint="unix:///s",
        old_pid=42,
        ping=lambda e: True,
        alive=lambda p: True,
    )
    assert res.get("ok") is False
    assert res.get("error_code") == "TIMEOUT"
    assert res.get("phase") == "reconcile_wait_old_exit"
    start_cmds = [c for c in fx.calls if "start" in c]
    assert start_cmds == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_success_shape_unchanged(tmp_path):
    from sw_core.setup_cmd import reconcile
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects(systemd=True)
    res = reconcile(
        old_mode="on-demand",
        target_mode="systemd-user",
        fx=fx,
        home=tmp_path,
        daemon_running=True,
        any_flashing=False,
        old_endpoint="unix:///s",
        old_pid=42,
        ping=lambda e: False,
        alive=lambda p: False,
    )
    assert "ok" not in res
    assert set(res.keys()) == {"mode", "transitioned", "applied", "ran", "pending_sudo"}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_waits_for_pid_when_ping_is_down(tmp_path, monkeypatch):
    import sw_core.setup_cmd as setup_cmd
    from sw_core.setup_cmd import reconcile
    from sw_core.sysenv import FakeEffects

    t = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return t[0]

        @staticmethod
        def sleep(seconds):
            t[0] += seconds

    monkeypatch.setattr(setup_cmd, "time", FakeTime)
    alive_calls = []
    effects = FakeEffects(systemd=True)
    result = reconcile(
        old_mode="on-demand",
        target_mode="systemd-user",
        fx=effects,
        home=tmp_path,
        daemon_running=False,
        old_endpoint="unix:///old.sock",
        old_pid=42,
        ping=lambda _endpoint: False,
        alive=lambda pid: (alive_calls.append(pid), True)[1],
    )

    assert result["error_code"] == "TIMEOUT"
    assert alive_calls and set(alive_calls) == {42}
    assert not any("start" in call for call in effects.calls)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_reconcile_known_pid_wins_over_inactive_systemd_status(tmp_path, monkeypatch):
    import sw_core.multi_open as multi_open
    import sw_core.setup_cmd as setup_cmd
    from sw_core.setup_cmd import reconcile
    from sw_core.sysenv import FakeEffects

    t = [0.0]

    class FakeTime:
        @staticmethod
        def monotonic():
            return t[0]

        @staticmethod
        def sleep(seconds):
            t[0] += seconds

    monkeypatch.setattr(setup_cmd, "time", FakeTime)
    monkeypatch.setattr(multi_open, "daemon_process_alive", lambda _pid, _endpoint: True)
    effects = FakeEffects(
        systemd=True,
        commands={("systemctl", "--user", "is-active", "serialwrap"): (3, "inactive", "")},
    )

    result = reconcile(
        old_mode="systemd-user",
        target_mode="on-demand",
        fx=effects,
        home=tmp_path,
        old_endpoint="unix:///old.sock",
        old_pid=42,
        ping=lambda _endpoint: False,
    )

    assert result["error_code"] == "TIMEOUT"
    assert not any(call[-2:] == ["is-active", "serialwrap"] for call in effects.calls)
    assert not any("start" in call for call in effects.calls)


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_socket_bound_with_restrictive_umask(tmp_path, monkeypatch):
    import asyncio
    from sw_core.rpc_posix import JsonRpcUnixServer

    real_umask = os.umask
    previous_umask = real_umask(0o177)
    sock = str(tmp_path / "umask.sock")
    srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True}, blocking_methods=set())

    async def run():
        await srv.start()
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        await srv.stop()
        return mode

    try:
        mode = asyncio.run(run())
    finally:
        real_umask(previous_umask)
    assert mode == 0o600


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_runtime_config_never_writes_system_fallback(tmp_path, monkeypatch):
    from sw_core import constants
    from sw_core.daemon import _write_config_endpoint

    cfg_dir = tmp_path / "user_cfg"
    monkeypatch.setattr(constants, "CONFIG_DIR", str(cfg_dir))

    sysf = tmp_path / "etc" / "config.yaml"
    sysf.parent.mkdir(parents=True, exist_ok=True)
    orig_content = "supervision_mode: on-demand\n"
    sysf.write_text(orig_content, encoding="utf-8")
    monkeypatch.setattr(constants, "SYSTEM_CONFIG_PATH", str(sysf), raising=False)

    _write_config_endpoint("unix:///new/endpoint.sock")

    assert (cfg_dir / "config.yaml").is_file()
    import yaml
    written = yaml.safe_load((cfg_dir / "config.yaml").read_text(encoding="utf-8"))
    assert written == {
        "supervision_mode": "on-demand",
        "socket_path": "unix:///new/endpoint.sock",
    }
    assert sysf.read_text(encoding="utf-8") == orig_content


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_daemon_process_alive_requires_complete_evidence(tmp_path, monkeypatch):
    import sw_core.multi_open as multi_open

    proc_root = tmp_path / "proc"
    (proc_root / "self").mkdir(parents=True)
    assert multi_open.daemon_process_alive(42, "/tmp/old.sock", proc_root=str(proc_root)) is False
    assert multi_open.daemon_process_alive(0, "/tmp/old.sock", proc_root=str(proc_root)) is False
    assert multi_open.daemon_process_alive(
        None, "/tmp/old.sock", proc_root=str(tmp_path / "missing")
    ) is None
    assert multi_open.daemon_process_alive(
        42, "/tmp/old.sock", proc_root=str(tmp_path / "missing")
    ) is None

    hidden = proc_root / "77"
    hidden.mkdir()
    real_open = open

    def guarded_open(path, *args, **kwargs):
        if str(path) == str(hidden / "cmdline"):
            raise PermissionError("cmdline denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(multi_open, "open", guarded_open, raising=False)
    assert multi_open.daemon_process_alive(None, "/tmp/old.sock", proc_root=str(proc_root)) is None


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_daemon_process_alive_matches_endpoint_when_pid_is_unknown(tmp_path):
    import sw_core.multi_open as multi_open

    proc_root = tmp_path / "proc"
    (proc_root / "self").mkdir(parents=True)
    daemon = proc_root / "123"
    daemon.mkdir()
    (daemon / "cmdline").write_bytes(
        b"python\0-m\0sw_core.daemon\0--socket\0/tmp/old.sock\0"
    )

    assert multi_open.daemon_process_alive(
        None, "unix:///tmp/x/../old.sock", proc_root=str(proc_root)
    ) is True


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_lock_fake_api_closes_and_releases_fd(tmp_path, monkeypatch):
    import sw_core.lock_win as lock_win
    import sw_core.win32_files as win32_files

    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda _endpoint: False)
    lock_calls = []
    monkeypatch.setattr(
        win32_files,
        "lock_file_ex",
        lambda fd, **_kwargs: (lock_calls.append(fd), False)[1],
    )
    contended = lock_win.WindowsSingletonLock(
        str(tmp_path / "contended.lock"), "tcp://127.0.0.1:48796"
    )
    with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
        contended.acquire()
    with pytest.raises(OSError):
        os.fstat(lock_calls[-1])

    unlocked = []
    monkeypatch.setattr(win32_files, "lock_file_ex", lambda fd, **_kwargs: True)
    monkeypatch.setattr(win32_files, "unlock_file_ex", lambda fd: unlocked.append(fd))
    acquired = lock_win.WindowsSingletonLock(
        str(tmp_path / "released.lock"), "tcp://127.0.0.1:48796"
    )
    acquired.acquire()
    fd = acquired._fd
    assert fd is not None
    acquired.release()
    assert unlocked == [fd]
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_restart_on_demand_shape(monkeypatch, capsys):
    import json
    import sw_core.cli as cli
    from sw_core.runtime_config import RuntimeConfig

    fake_rc = RuntimeConfig.__new__(RuntimeConfig)
    fake_rc._data = {"supervision_mode": "on-demand", "socket_path": "/run/s.sock"}
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: fake_rc)

    monkeypatch.setattr(cli, "_run_daemon_stop", lambda args: 0)
    monkeypatch.setattr(cli, "wait_old_daemon_gone", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(cli, "_run_daemon_start", lambda args: 0)

    rc = cli.main(["daemon", "restart"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data.get("ok") is True
    assert data.get("restarted") is True
    assert data.get("stopped") is True
    assert "start" in data


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_restart_starts_same_endpoint_with_start_args(monkeypatch, capsys):
    import json
    import sw_core.cli as cli
    from sw_core.runtime_config import RuntimeConfig

    endpoint = "/tmp/custom/serialwrap.sock"
    fake_rc = RuntimeConfig.__new__(RuntimeConfig)
    fake_rc._data = {"supervision_mode": "on-demand", "socket_path": endpoint}
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: fake_rc)
    monkeypatch.setattr(
        cli,
        "_resolve_local_endpoint_info",
        lambda _args: cli._ResolvedEndpoint(endpoint, "config.yaml"),
    )
    monkeypatch.setattr(cli, "_run_daemon_stop", lambda _args: 0)
    wait_calls = []
    monkeypatch.setattr(
        cli,
        "wait_old_daemon_gone",
        lambda old_endpoint, pid, **_kwargs: (wait_calls.append((old_endpoint, pid)), True)[1],
    )
    traced_calls = []

    def rpc_traced(_args, _resolved, method, _params, **_kwargs):
        traced_calls.append(method)
        if method == "health.status":
            return {"ok": True, "pid": 123}
        return {"ok": True, "warnings": []}

    monkeypatch.setattr(cli, "_rpc_call_traced", rpc_traced)
    monkeypatch.setattr(cli, "_call_rpc_endpoint", lambda *_a, **_kw: {"ok": False})
    monkeypatch.setattr(cli, "_find_conflicting_daemon", lambda _socket: None)
    monkeypatch.setattr(cli, "daemon_launch_argv", lambda **_kwargs: ["/fake/serialwrapd"])
    monkeypatch.setattr(cli, "_resolve_daemon_start_env_files", lambda _profile: [])
    monkeypatch.setattr(cli, "_load_daemon_start_env_files", lambda _files: (dict(os.environ), []))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    spawned = []

    class FakeProcess:
        pid = 456

        @staticmethod
        def poll():
            return None

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    rc = cli.main(["daemon", "restart"])

    assert rc == 0
    assert wait_calls == [(endpoint, 123)]
    assert "health.status" in traced_calls
    argv = spawned[0][0]
    assert argv[0] == "/fake/serialwrapd"
    assert argv[argv.index("--socket") + 1] == endpoint
    assert argv[argv.index("--profile-dir") + 1] == cli.PROFILE_DIR
    assert argv[argv.index("--lock") + 1] == cli.LOCK_PATH
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_restart_timeout_does_not_start(monkeypatch, capsys):
    import json
    import sw_core.cli as cli
    from sw_core.runtime_config import RuntimeConfig

    fake_rc = RuntimeConfig.__new__(RuntimeConfig)
    fake_rc._data = {"supervision_mode": "on-demand", "socket_path": "/tmp/old.sock"}
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: fake_rc)
    monkeypatch.setattr(
        cli,
        "_resolve_local_endpoint_info",
        lambda _args: cli._ResolvedEndpoint("/tmp/old.sock", "config.yaml"),
    )
    monkeypatch.setattr(cli, "_rpc_call_traced", lambda *_a, **_kw: {"ok": True, "pid": 42})
    monkeypatch.setattr(cli, "_run_daemon_stop", lambda _args: 0)
    monkeypatch.setattr(cli, "wait_old_daemon_gone", lambda *_a, **_kw: False)
    starts = []
    monkeypatch.setattr(cli, "_run_daemon_start", lambda _args: starts.append(True))

    rc = cli.main(["daemon", "restart"])

    assert rc == 1
    assert starts == []
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error_code": "TIMEOUT",
        "phase": "restart_wait_old_exit",
    }


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize(
    "argv",
    [
        ["--bench", "B1", "daemon", "restart"],
        ["--endpoint", "tcp://127.0.0.1:48755", "daemon", "restart"],
    ],
)
def test_daemon_restart_rejects_nonlocal_target_before_stop(argv, monkeypatch, capsys):
    import json
    import sw_core.cli as cli
    from sw_core.runtime_config import RuntimeConfig

    fake_rc = RuntimeConfig.__new__(RuntimeConfig)
    fake_rc._data = {"supervision_mode": "on-demand"}
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: fake_rc)
    rpc_calls = []
    stop_calls = []
    monkeypatch.setattr(cli, "_rpc_call_traced", lambda *a, **kw: rpc_calls.append(a))
    monkeypatch.setattr(cli, "_run_daemon_stop", lambda _args: stop_calls.append(True))
    monkeypatch.setattr(cli, "_rpc_backend_is_win", lambda: False)

    rc = cli.main(argv)

    assert rc == 2
    assert rpc_calls == []
    assert stop_calls == []
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error_code": "INVALID_ARGS",
        "message": "daemon restart 只能重啟本機 daemon（不支援 --endpoint／--bench）",
    }


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_restart_systemd_routes_to_service_restart(monkeypatch):
    import sw_core.cli as cli
    from sw_core.runtime_config import RuntimeConfig

    fake_rc = RuntimeConfig.__new__(RuntimeConfig)
    fake_rc._data = {"supervision_mode": "systemd-user"}
    monkeypatch.setattr(cli, "_safe_runtime_config", lambda: fake_rc)

    service_action_calls = []
    monkeypatch.setattr(
        cli,
        "service_action",
        lambda action, mode=None, with_sudo=False: (
            service_action_calls.append((action, mode, with_sudo)),
            {"ok": True},
        )[1],
    )

    rc = cli.main(["daemon", "restart"])
    assert rc == 0
    assert len(service_action_calls) == 1
    assert service_action_calls[0][0] == "restart"
    assert service_action_calls[0][1] == "systemd-user"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_service_start_scans_before_watcher_thread(tmp_path):
    from unittest.mock import MagicMock
    from sw_core.service import SerialwrapService

    calls = []
    service = SerialwrapService([])

    watcher = service._watcher
    sessions = service._sessions

    watcher.scan_once = lambda: (calls.append("scan_once"), {})[1]
    sessions.prepare_dynamic_rank = lambda devs: calls.append("prepare_dynamic_rank")
    sessions.update_devices = lambda devs: calls.append("update_devices")
    sessions.bootstrap_attach = lambda: calls.append("bootstrap_attach")
    watcher.start = lambda: calls.append("watcher.start")

    service._engine.start = MagicMock()
    service._flash_endpoint.start = MagicMock()

    service.start()
    assert calls == ["scan_once", "prepare_dynamic_rank", "update_devices", "bootstrap_attach", "watcher.start"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_runtime_config_reads_via_resolve_config_path(tmp_path, monkeypatch):
    from sw_core.daemon import _write_config_endpoint
    from sw_core.runtime_config import RuntimeConfig

    custom_cfg = tmp_path / "custom_config.yaml"
    monkeypatch.setenv("SERIALWRAP_CONFIG", str(custom_cfg))

    _write_config_endpoint("unix:///test_custom.sock")

    assert custom_cfg.is_file()
    rc = RuntimeConfig(str(custom_cfg))
    assert rc.socket_path() == "unix:///test_custom.sock"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_readme_mentions_daemon_restart():
    readme_path = Path(__file__).resolve().parents[1] / "README.md"
    content = readme_path.read_text(encoding="utf-8")
    assert "## 繁體中文" in content
    en_section, zh_section = content.split("## 繁體中文", 1)
    assert "serialwrap daemon restart" in en_section or "`daemon restart`" in en_section
    assert "serialwrap daemon restart" in zh_section or "`daemon restart`" in zh_section
