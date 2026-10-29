"""S1X-a-5：舊 pid 未知時的全行程掃描不得被他人／無 `--socket` 的 serialwrapd 卡成 None。

Stage 1 出口審查 A 指出 `daemon_process_alive(pid=None, …)` 只要掃到任一不帶
`--socket` 的 serialwrapd（systemd-user unit 的 ExecStart 正是如此）或任一 cmdline 讀取
失敗就回 `None`；`wait_old_daemon_gone` 只接受 `False`，因此 setup 模式切換與
`daemon restart` 在共用機上 10 秒後回 TIMEOUT（0.3.2 會完成）。

修正：略過 uid ≠ 自身的行程；同 uid 但不帶 `--socket` 的 daemon 以其預設 endpoint
（`DEFAULT_ENDPOINT`）比對；讀取失敗只在同 uid 行程上視為掃描不完整。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import sw_core.multi_open as multi_open
from sw_core.constants import DEFAULT_ENDPOINT

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]

# Windows 無 os.getuid：模組須仍可收集（各測試另以 posix_only 標記）。
OTHER_UID = (os.getuid() if hasattr(os, "getuid") else 0) + 4242


def _proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    return root


def _daemon(root: Path, pid: int, cmdline: bytes) -> Path:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(cmdline)
    return d


def _uids(monkeypatch: pytest.MonkeyPatch, mapping: dict[int, int]) -> None:
    real = multi_open._proc_uid

    def fake(proc_root: str, pid: int) -> int | None:
        if pid in mapping:
            return mapping[pid]
        return real(proc_root, pid)

    monkeypatch.setattr(multi_open, "_proc_uid", fake)


SYSTEMD_USER_CMDLINE = b"/usr/bin/python3\0/opt/other-venv/bin/serialwrapd\0"


def test_other_users_daemon_without_socket_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """他人以 systemd-user（無 --socket）執行的 daemon 不影響本使用者的判定：回 False。"""
    root = _proc(tmp_path)
    _daemon(root, 200, SYSTEMD_USER_CMDLINE)
    _uids(monkeypatch, {200: OTHER_UID})

    assert multi_open.daemon_process_alive(None, "/tmp/mine.sock", proc_root=str(root)) is False


def test_other_users_unreadable_cmdline_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """他 uid 行程 cmdline 不可讀（hidepid 等）不視為掃描不完整。"""
    root = _proc(tmp_path)
    hidden = root / "300"
    hidden.mkdir()
    _uids(monkeypatch, {300: OTHER_UID})
    real_open = open

    def guarded_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == str(hidden / "cmdline"):
            raise PermissionError("cmdline denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(multi_open, "open", guarded_open, raising=False)

    assert multi_open.daemon_process_alive(None, "/tmp/mine.sock", proc_root=str(root)) is False


def test_same_uid_daemon_without_socket_uses_default_endpoint(tmp_path: Path) -> None:
    """同 uid 但不帶 --socket 的 daemon 綁預設 endpoint：與之相同才算存活。"""
    root = _proc(tmp_path)
    _daemon(root, 400, b"/usr/bin/python3\0/opt/my-venv/bin/serialwrapd\0")

    assert multi_open.daemon_process_alive(None, DEFAULT_ENDPOINT, proc_root=str(root)) is True
    assert multi_open.daemon_process_alive(None, "/tmp/elsewhere.sock", proc_root=str(root)) is False


def test_wait_old_daemon_gone_completes_with_foreign_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """舊 daemon 已停止且 pid 未知、主機上另有他人 daemon：wait_old_daemon_gone 立即成功，不逾時。"""
    from sw_core.setup_cmd import wait_old_daemon_gone

    root = _proc(tmp_path)
    _daemon(root, 200, SYSTEMD_USER_CMDLINE)
    _uids(monkeypatch, {200: OTHER_UID})
    clock = [0.0]

    def sleep(dt: float) -> None:
        clock[0] += dt

    ok = wait_old_daemon_gone(
        "/tmp/mine.sock",
        None,
        timeout_s=10.0,
        ping=lambda _ep: False,
        alive=lambda pid: multi_open.daemon_process_alive(pid, "/tmp/mine.sock", proc_root=str(root)),
        sleep=sleep,
        monotonic=lambda: clock[0],
    )

    assert ok is True
    assert clock[0] == 0.0
