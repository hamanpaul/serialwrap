# tests/test_p1oc_socket_perm.py
"""Task P1-OC-T3：SC-08 撤回（OD-14 #3），socket 權限還原 0660 基線的失敗測試。

依規格：bind 後 socket 維持 `0o660`；`SERIALWRAP_SOCKET_GROUP` 有值時 chgrp 至該群組，
群組不存在則忽略。`ensure_runtime_dirs()` 新建目錄沿用 process umask（不強制 0700），
`JsonRpcUnixServer.start()` 期間不得切換 umask。

本模組不在頂層 import 本 Phase 新增的名稱；僅 import P1 既有的 `JsonRpcUnixServer`。
"""
from __future__ import annotations

import asyncio
import grp
import os
import stat

import pytest

from sw_core.rpc_posix import JsonRpcUnixServer

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]


def _mode_after_start(path: str) -> int:
    """啟動 server 後讀取 socket 權限位元（僅權限，不含檔案型別）。"""

    async def run() -> int:
        srv = JsonRpcUnixServer(path, lambda m, p: {"ok": True})
        await srv.start()
        try:
            return stat.S_IMODE(os.stat(path).st_mode)
        finally:
            await srv.stop()

    return asyncio.run(run())


def test_socket_mode_0660_without_group(tmp_path, monkeypatch):
    monkeypatch.delenv("SERIALWRAP_SOCKET_GROUP", raising=False)
    assert _mode_after_start(str(tmp_path / "s.sock")) == 0o660


def test_socket_group_chgrp(tmp_path, monkeypatch):
    g = grp.getgrgid(os.getgid()).gr_name
    # 前後空白需被 strip，群組名稱才能解析成功
    monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", f"  {g}  ")
    p = str(tmp_path / "s.sock")

    async def run():
        srv = JsonRpcUnixServer(p, lambda m, q: {"ok": True})
        await srv.start()
        try:
            st = os.stat(p)
            return stat.S_IMODE(st.st_mode), st.st_gid
        finally:
            await srv.stop()

    mode, gid = asyncio.run(run())
    assert mode == 0o660 and gid == os.getgid()


def test_socket_group_unknown_ignored(tmp_path, monkeypatch):
    # 不存在的群組：chgrp 失敗應被忽略，權限仍為 0660
    monkeypatch.setenv("SERIALWRAP_SOCKET_GROUP", "no-such-group-p1oc")
    assert _mode_after_start(str(tmp_path / "s.sock")) == 0o660


def test_runtime_dirs_not_forced_0700(tmp_path, monkeypatch):
    from sw_core import constants

    # 六個受管目錄皆指向不存在的 tmp 路徑，確保走「新建」分支
    dirs = {
        "CONFIG_DIR": tmp_path / "cfg_new",
        "STATE_DIR": tmp_path / "state_new",
        "RUN_DIR": tmp_path / "run_new",
        "WAL_DIR": tmp_path / "wal_new",
        "EVENTS_DIR": tmp_path / "events_new",
        "EVENTS_RUNTIME_DIR": tmp_path / "events_rt_new",
    }
    for name, path in dirs.items():
        monkeypatch.setattr(constants, name, str(path))

    original_umask = os.umask(0o022)
    try:
        constants.ensure_runtime_dirs()
    finally:
        os.umask(original_umask)

    # 新建目錄權限由 umask 決定（0o777 & ~0o022 = 0o755），不得被強制為 0o700
    for name, path in dirs.items():
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o755, name


def test_no_umask_switch_in_start(tmp_path, monkeypatch):
    calls: list[int] = []
    real_umask = os.umask

    def recording_umask(mask: int) -> int:
        calls.append(mask)
        return real_umask(mask)

    monkeypatch.setattr(os, "umask", recording_umask)
    monkeypatch.delenv("SERIALWRAP_SOCKET_GROUP", raising=False)
    sock = str(tmp_path / "u.sock")

    async def run() -> int:
        srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True})
        before = len(calls)
        await srv.start()
        try:
            return len(calls) - before
        finally:
            await srv.stop()

    umask_calls_during_start = asyncio.run(run())
    assert umask_calls_during_start == 0
