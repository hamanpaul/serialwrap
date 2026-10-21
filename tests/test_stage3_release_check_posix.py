# tests/test_stage3_release_check_posix.py
"""P9-T4 PosixProbe 實測（/proc 程序樹、PTY holder、flock 鎖、AF_UNIX 端點）的紅燈測試。

本檔為 posix_only：Windows 上 conftest 的 pytest_ignore_collect 會排除整個模組。
本檔會開 PTY、啟動子程序與 AF_UNIX 監聽，故 posix_pty／process_rpc 於模組層宣告（不得在項目層再加資源 marker）。
實作尚未存在時，各測試於 _rc() 處以 ModuleNotFoundError 失敗（延遲 import，collect-only 不受影響）。
"""
from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]


def _rc() -> Any:
    return importlib.import_module("tools.stage3.release_check")


def test_posix_probe_sees_pty_holder_then_release():
    import pty

    rc = _rc()
    master, slave = pty.openpty()
    slave_path = os.ttyname(slave)
    os.close(slave)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import os,time; fd=os.open({slave_path!r}, os.O_RDWR|os.O_NOCTTY); time.sleep(30)",
        ]
    )
    probe = rc.default_probe()
    try:
        deadline = time.monotonic() + 5
        while child.pid not in (probe.device_holders(slave_path) or ((), 0))[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.pid in probe.device_holders(slave_path)[0]
        assert probe.pid_alive(child.pid) is True
        child.kill()
        child.wait()
        assert probe.pid_alive(child.pid) is False
        assert child.pid not in (probe.device_holders(slave_path) or ((), 0))[0]
    finally:
        if child.poll() is None:
            child.kill()
        os.close(master)


def test_posix_unreadable_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    probe = _rc().PosixProbe()
    dev = tmp_path / "fake-dev"
    dev.write_text("", encoding="utf-8")
    proc_root = tmp_path / "proc"
    holder_fd_dir = proc_root / "100" / "fd"
    denied_fd_dir = proc_root / "200" / "fd"
    holder_fd_dir.mkdir(parents=True)
    denied_fd_dir.mkdir(parents=True)
    (holder_fd_dir / "7").symlink_to(dev)
    probe._PROC_ROOT = str(proc_root)
    real_scandir = os.scandir

    def _scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path) == str(denied_fd_dir):
            raise PermissionError(13, "Permission denied", str(denied_fd_dir))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _scandir)
    assert probe.device_holders(str(dev)) == ((100,), 1)


def test_posix_descendants_skips_process_that_exits_during_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    probe = _rc().PosixProbe()
    proc_root = tmp_path / "proc"
    (proc_root / "100").mkdir(parents=True)
    (proc_root / "101").mkdir()
    (proc_root / "102").mkdir()
    (proc_root / "100" / "stat").write_text("100 (daemon) S 1 0\n", encoding="utf-8")
    (proc_root / "101" / "stat").write_text("101 (child) S 100 0\n", encoding="utf-8")
    (proc_root / "102" / "stat").write_text("102 (vanishing) S 100 0\n", encoding="utf-8")
    probe._PROC_ROOT = str(proc_root)
    original_read_bytes = Path.read_bytes

    def vanishing_stat(path: Path, *args: Any, **kwargs: Any) -> bytes:
        if path == proc_root / "102" / "stat":
            raise ProcessLookupError(3, "process disappeared", str(path))
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", vanishing_stat)
    assert probe.descendants(100) == (101,)


def test_posix_lock_probe(tmp_path: Path):
    probe = _rc().PosixProbe()
    lock = tmp_path / "serialwrapd.lock"
    code = (
        "import fcntl,sys,time\n"
        "f=open(sys.argv[1],'a+b')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        assert probe.lock_acquirable(str(lock)) is False
    finally:
        child.kill()
        child.wait()
    assert probe.lock_acquirable(str(lock)) is True


def test_posix_unix_socket_probe(tmp_path: Path):
    probe = _rc().PosixProbe()
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        assert probe.endpoint_connectable(path) is True
    finally:
        srv.close()
        if os.path.exists(path):
            os.unlink(path)
    assert probe.endpoint_connectable(path) is False


def test_posix_pids_by_exe_matches_cmdline_script(tmp_path: Path):
    probe = _rc().PosixProbe()
    script = tmp_path / "serialwrapd.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    match = [os.path.realpath(script)]
    child = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 5
        while child.pid not in (probe.pids_by_exe(match) or ()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.pid in probe.pids_by_exe(match)
    finally:
        child.kill()
        child.wait()
