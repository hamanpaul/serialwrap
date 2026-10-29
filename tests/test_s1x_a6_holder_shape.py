"""S1X-a-6：一般使用者 daemon 的 `foreign_holders` 形狀回到 0.3.2，且兩個實機 case 接受 dict entry。

Stage 1 出口審查 A 指出：非 root 時 `probe_holders` 必因他 uid 行程的 `/proc/<pid>/fd` 不可讀
而回 `status=permission`，每次開埠都 `_set_holder_unknown(True)` 並寫 WARNING，`daemon status`
／`doctor` 的 `foreign_holders` 對每個已掛載裝置都由 `{dev: pid}` 變成
`{dev: {"device":…,"holder_unknown":true}}`；`f08_daemon_singleton` 因 `int(dict)` 拋 TypeError，
`realhw/cases/p0.py` 以 `p != own_pid` 比對 dict 得到假 FAIL。

修正：
- 掃描不完整只因他 uid 行程不可讀時記為 `status=partial_other_uid`、不升為 holder_unknown、
  不寫 WARNING（同 uid 行程皆可讀時，serialwrap 對自身使用者的互斥保證並未減弱）。
- f08／p0 改為接受 dict entry（比照 `realhw/acceptance/gate.py::_foreign_holder_violations`）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "regression"))

from test_holder_probe_namespace import (  # noqa: E402
    HOST_DEV,
    PTS7_RDEV,
    TTY,
    FakeProcNs,
    _probe,
    proc_root,  # noqa: F401 — pytest fixture
)

OTHER_UID = os.getuid() + 4242


def _owner(monkeypatch: pytest.MonkeyPatch, mapping: dict[int, int]) -> None:
    from sw_core import serial_port as sp

    real = sp._proc_owner_uid

    def fake(proc: str, pid: int) -> int | None:
        if pid in mapping:
            return mapping[pid]
        return real(proc, pid)

    monkeypatch.setattr(sp, "_proc_owner_uid", fake)


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_other_uid_unreadable_fd_is_partial_not_unknown(
    proc_root: FakeProcNs, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """只有他 uid 行程的 fd 不可讀：status=partial_other_uid、holder_unknown=False。"""
    proc_root.add_fd(7001, 3, TTY, PermissionError(13, "denied", TTY))
    proc_root.add_fd(7002, 3, "/dev/null", (HOST_DEV, 99))
    _owner(monkeypatch, {7001: OTHER_UID})

    rep = _probe(str(proc_root.root))

    assert rep.pids == ()
    assert rep.status == "partial_other_uid"
    assert rep.holder_unknown is False


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_other_uid_fd_dir_unlistable_is_partial_not_unknown(
    proc_root: FakeProcNs, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """他 uid 行程的 fd 目錄無法列舉（一般使用者對 /proc/1/fd 的常態）：同樣不升為 holder_unknown。"""
    fd_dir = proc_root.add_proc(1)
    real_listdir = os.listdir

    def listdir(path: Any) -> list[str]:
        if os.fspath(path) == fd_dir:
            raise PermissionError(13, "denied", fd_dir)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    _owner(monkeypatch, {1: OTHER_UID})

    rep = _probe(str(proc_root.root))

    assert rep.status == "partial_other_uid"
    assert rep.holder_unknown is False


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_same_uid_unreadable_fd_stays_unknown(
    proc_root: FakeProcNs, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """同 uid 行程不可讀仍為 permission／holder_unknown（BC-04 既有語意）。"""
    proc_root.add_fd(7101, 3, TTY, PermissionError(13, "denied", TTY))
    proc_root.add_fd(7102, 3, TTY, PermissionError(13, "denied", TTY))
    _owner(monkeypatch, {7101: OTHER_UID, 7102: os.getuid()})

    rep = _probe(str(proc_root.root))

    assert rep.status == "permission"
    assert rep.holder_unknown is True


@pytest.mark.posix_only
@pytest.mark.posix_pty
def test_open_with_partial_other_uid_scan_is_not_holder_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """開埠時掃描只缺他 uid 行程：不寫 WARNING、不列入 unknown_holder_devices（foreign_holders 回 0.3.2 形狀）。"""
    import pty

    from sw_core import serial_port as sp
    from sw_core.config import UartProfile

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    report = sp.HolderReport((), False, "partial_other_uid")
    monkeypatch.setattr(sp, "probe_holders", lambda *a, **k: report)
    port = sp._PosixSerialPort(path, UartProfile())
    try:
        with caplog.at_level("WARNING", logger="serialwrap"):
            port.open()
        assert "無法確認" not in caplog.text
        assert os.path.realpath(path) not in sp.unknown_holder_devices()
    finally:
        port.close()
        os.close(master)
        os.close(slave)


class _Sw:
    def __init__(self, status: dict[str, Any], by_id: str = "/dev/null") -> None:
        self._status = status
        self._by_id = by_id

    def run(self, *args: str) -> dict[str, Any]:
        if args[:2] == ("daemon", "status"):
            return self._status
        if args[:1] == ("doctor",):
            return {"checks": [{"check": "single_daemon", "ok": True}]}
        raise AssertionError(args)

    def session(self, _com: str) -> dict[str, Any]:
        return {"device_by_id": self._by_id}


def _ctx(status: dict[str, Any]) -> Any:
    return SimpleNamespace(
        sw=_Sw(status),
        cfg={"boards": [{"com": "COM0"}]},
        note=lambda *_a, **_k: None,
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize(
    ("entry", "verdict"),
    [
        (4321, "PASS"),
        ({"device": "/dev/null", "holder_unknown": True, "pids": [4321]}, "PASS"),
        ({"device": "/dev/null", "holder_unknown": True, "pids": [999]}, "FAIL"),
        ({"device": "/dev/null", "holder_unknown": True}, "FAIL"),
    ],
    ids=["int", "dict-own-pid", "dict-other-pid", "dict-no-pid"],
)
def test_f08_tty_holders_reported_accepts_dict_entry(entry: Any, verdict: str) -> None:
    """f08 對 dict entry 不得拋 TypeError：pids 為主 daemon pid 才 PASS，其餘依既有語意 FAIL。"""
    from serialwrap_regression.cases import f08_daemon_singleton as f08

    status = {"pid": 4321, "foreign_holders": {os.path.realpath("/dev/null"): entry}}
    result = f08.f8_tty_holders_reported(_ctx(status))
    assert result.verdict == verdict, result


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize(
    ("entry", "verdict"),
    [
        (4321, "PASS"),
        ({"device": "/dev/pts/0", "holder_unknown": True}, "PASS"),
        ({"device": "/dev/pts/0", "holder_unknown": True, "pids": [4321]}, "PASS"),
        ({"device": "/dev/pts/0", "holder_unknown": True, "pids": [999]}, "FAIL"),
        (999, "FAIL"),
    ],
    ids=["int-own", "dict-unknown", "dict-own-pid", "dict-other-pid", "int-other"],
)
def test_p0_multiopen_accepts_dict_entry(entry: Any, verdict: str) -> None:
    """p0-multiopen 對 holder_unknown dict entry 不得假 FAIL；只有非本 daemon 的 pid 才算外來持有者。"""
    from realhw.cases import p0

    status = {"pid": 4321, "multi_open": False, "foreign_holders": {"/dev/pts/0": entry}}
    result = p0.p0_multiopen(_ctx(status))
    assert result.verdict == verdict, result
