"""holder 探測的 mount namespace 碰撞回歸（#R2，pytest 覆蓋；不需 regression/ case）。

背景：docker 容器內以獨立 devpts instance 開出的 ``/dev/pts/N``，``readlink`` 字串與主機的
``/dev/pts/N`` 相同、``st_rdev``（136:N）也相同，但 ``(st_dev, st_ino)`` 不同（主機 0:60、容器
0:140／0:148）。舊版 ``probe_holders``、``UARTBridge._enumerate_all_held_paths`` 與 ``detect_multi_open``
以字串或 rdev 判定，會把容器的 fd 誤判為主機 tty 的 holder（探測層機制已以受控 namespace 重現）。
注意：G4（``test_cli_mgmt_update``）為 ``pidns_rig``，其 daemon 位於獨立 pid namespace、看不到主機行程，
故該間歇失敗的根因不屬於本碰撞；本檔不宣稱其已被涵蓋。

本檔以 ``proc_root`` fixture 佈置假 ``/proc``，並 monkeypatch ``os.stat`` 與 ``os.readlink``：
fd 的 readlink 字串與 inode 身分可獨立指定，因此能模擬「字串相同、inode 不同」的外部 namespace fd，
以及「同一 inode」的真實 holder。這些情境在真機上難以穩定重現（依主機 pty 配號而定），故以 pytest 固定。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.memory_only]

# 主機 devpts（st_dev=0:60）與容器 devpts（0:140、0:148）：同名 /dev/pts/7 實為不同裝置。
HOST_DEV = 60
CONTAINER_DEV_A = 140
CONTAINER_DEV_B = 148
# 136:7 的 rdev 在所有 devpts instance 皆相同，用來證明 rdev 不再是身分證據。
PTS7_RDEV = (136 << 8) | 7
TTY = "/dev/pts/7"


class _Stat:
    """與 os.stat_result 相容的最小替身（st_dev／st_ino／st_mode，可選 st_rdev）。"""

    def __init__(self, dev: int, ino: int, rdev: int | None = None) -> None:
        self.st_dev = dev
        self.st_ino = ino
        self.st_mode = stat.S_IFCHR | 0o620
        if rdev is not None:
            self.st_rdev = rdev


class FakeProcNs:
    """假 /proc：每條 fd 的 readlink 字串與 stat 身分各自可控。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.links: dict[str, str] = {}
        self.stats: dict[str, object] = {}

    def add_proc(self, pid: int, cmdline: str | None = None) -> str:
        """建立假行程目錄；cmdline 僅在明確指定或首次建立時寫入（避免 add_fd 覆寫既有身分）。"""
        pdir = self.root / str(pid)
        (pdir / "fd").mkdir(parents=True, exist_ok=True)
        if cmdline is not None or not (pdir / "cmdline").exists():
            (pdir / "cmdline").write_bytes((cmdline or "bash\0").encode())
        return str(pdir / "fd")

    def add_fd(self, pid: int, fd: int, link: str, identity, *, rdev: int | None = None) -> None:
        fd_dir = self.add_proc(pid)
        path = os.path.join(fd_dir, str(fd))
        Path(path).touch()
        self.links[path] = link
        self.stats[path] = identity if isinstance(identity, BaseException) else _Stat(*identity, rdev=rdev)

    def set_path(self, path: str, identity, *, rdev: int | None = None) -> None:
        self.stats[path] = identity if isinstance(identity, BaseException) else _Stat(*identity, rdev=rdev)


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeProcNs:
    fake = FakeProcNs(tmp_path / "proc")
    fake.root.mkdir()
    real_stat = os.stat
    real_readlink = os.readlink

    def _stat(path, *args, **kwargs):
        key = os.fspath(path)
        if key in fake.stats:
            val = fake.stats[key]
            if isinstance(val, BaseException):
                raise val
            return val
        return real_stat(path, *args, **kwargs)

    def _readlink(path, *args, **kwargs):
        key = os.fspath(path)
        if key in fake.links:
            return fake.links[key]
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)
    monkeypatch.setattr(os, "readlink", _readlink)
    # 主機 tty 的身分（TTY 不存在於 CI 主機，故一律由假表提供）。
    fake.set_path(TTY, (HOST_DEV, 10), rdev=PTS7_RDEV)
    return fake


def _probe(proc: str, path: str = TTY):
    from sw_core import serial_port as sp

    # probe_holders 在 os.name == "nt" 時直接回 unsupported；本檔驗證的是 procfs 比對，故於呼叫期間釘住 posix。
    with mock.patch.object(os, "name", "posix"):
        return sp.probe_holders(path, proc_root=proc, exclude_pid=None)


class TestProbeHoldersNamespace:
    def test_foreign_namespace_same_string_not_holder(self, proc_root: FakeProcNs) -> None:
        """容器（不同 devpts instance）的 /dev/pts/7 字串與主機相同、rdev 亦同，但 inode 不同 → 非 holder。"""
        proc_root.add_fd(4321, 3, TTY, (CONTAINER_DEV_B, 10), rdev=PTS7_RDEV)

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()
        assert rep.status == "ok"
        assert rep.holder_unknown is False

    def test_rdev_equal_alias_other_instance_not_holder(self, proc_root: FakeProcNs) -> None:
        """字串不同（別名）、rdev 相同、st_dev 不同（容器 0:140）→ 非 holder；rdev 不再是身分證據。"""
        proc_root.add_fd(4322, 3, "/dev/pts/ptmx-alias", (CONTAINER_DEV_A, 10), rdev=PTS7_RDEV)

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()

    def test_same_inode_holder_still_counts(self, proc_root: FakeProcNs) -> None:
        """同一 inode（同一 devpts instance、同一 pty）的真實 holder 仍必須被偵測。"""
        proc_root.add_fd(4323, 3, TTY, (HOST_DEV, 10), rdev=PTS7_RDEV)

        rep = _probe(str(proc_root.root))

        assert rep.pids == (4323,)
        assert rep.status == "ok"

    def test_alias_link_same_inode_counts(self, proc_root: FakeProcNs) -> None:
        """fd 經由不同路徑字串（別名）指向同一 inode → 仍為 holder（以 inode 而非字串判定）。"""
        proc_root.add_fd(4324, 3, "/dev/serial/by-id/usb-alias", (HOST_DEV, 10), rdev=PTS7_RDEV)

        rep = _probe(str(proc_root.root))

        assert rep.pids == (4324,)

    def test_string_only_match_without_identity_not_holder(self, proc_root: FakeProcNs) -> None:
        """字串相同但 fd 的 stat 無法取得（不可證明同一 inode）→ 不計入，亦不以字串補判。"""
        proc_root.add_fd(4325, 3, TTY, FileNotFoundError(2, "gone", TTY))

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()

    def test_mixed_foreign_and_true_holder_reports_only_true(self, proc_root: FakeProcNs) -> None:
        """同機同時存在容器假 holder 與真 holder 時，只回報真 holder。"""
        proc_root.add_fd(5001, 3, TTY, (CONTAINER_DEV_A, 10), rdev=PTS7_RDEV)
        proc_root.add_fd(5002, 3, TTY, (HOST_DEV, 10), rdev=PTS7_RDEV)

        rep = _probe(str(proc_root.root))

        assert rep.pids == (5002,)

    def test_permission_status_semantics_preserved(self, proc_root: FakeProcNs) -> None:
        """fd 讀不到（權限）→ status=permission；無 holder 時 holder_unknown 為 True（既有語意不變）。"""
        proc_root.add_fd(6001, 3, TTY, PermissionError(13, "denied", TTY))

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()
        assert rep.status == "permission"
        assert rep.holder_unknown is True

    def test_target_permission_failure_is_unknown_not_safe(self, proc_root: FakeProcNs) -> None:
        """Codex P2：目標 tty 的 stat 因權限失敗時，不得回「無 holder、status=ok」（那會讓 open 路徑誤清 unknown）。"""
        proc_root.set_path(TTY, PermissionError(13, "denied", TTY))

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()
        assert rep.status == "permission"
        assert rep.holder_unknown is True

    def test_target_missing_is_unknown_not_safe(self, proc_root: FakeProcNs) -> None:
        """Codex P2：目標 tty 不存在或無法 stat（非權限錯誤）→ status=unknown、holder_unknown=True，不得推論為安全。"""
        proc_root.set_path(TTY, FileNotFoundError(2, "missing", TTY))

        rep = _probe(str(proc_root.root))

        assert rep.pids == ()
        assert rep.status == "unknown"
        assert rep.holder_unknown is True

    def test_mknod_alias_non_devpts_same_rdev_counts(self, proc_root: FakeProcNs) -> None:
        """Codex P1／#R2：非 devpts 字元裝置經 mknod 別名（inode 不同、rdev 相同且 major=4）仍為同一 UART holder。"""
        uart = "/dev/ttyS0"
        uart_rdev = (4 << 8) | 64
        proc_root.set_path(uart, (HOST_DEV, 11), rdev=uart_rdev)
        proc_root.add_fd(4340, 3, uart, (CONTAINER_DEV_A, 55), rdev=uart_rdev)

        rep = _probe(str(proc_root.root), path=uart)

        assert rep.pids == (4340,)
        assert rep.holder_unknown is False


class TestUartBridgeHeldPathsNamespace:
    """UARTBridge._enumerate_all_held_paths 的 peer 判準與 probe 同源（#R2）。"""

    def _held(self, proc: str) -> set[str] | None:
        from sw_core import uart_io

        # 方法不讀取 self 的任何屬性，僅需 proc_root 注入；以替身傳入即可。
        return uart_io.UARTBridge._enumerate_all_held_paths(SimpleNamespace(), proc_root=proc)

    def test_foreign_namespace_string_not_in_held(self, proc_root: FakeProcNs) -> None:
        proc_root.add_fd(4411, 3, TTY, (CONTAINER_DEV_B, 10), rdev=PTS7_RDEV)

        held = self._held(str(proc_root.root))

        assert held is not None
        assert TTY not in held

    def test_same_inode_string_in_held(self, proc_root: FakeProcNs) -> None:
        proc_root.add_fd(4412, 3, TTY, (HOST_DEV, 10), rdev=PTS7_RDEV)

        held = self._held(str(proc_root.root))

        assert held is not None
        assert TTY in held

    def test_non_devpts_mknod_alias_in_held(self, proc_root: FakeProcNs) -> None:
        """Codex P1／#R2：非 devpts 字元裝置的 mknod 別名（inode 不同、rdev 相同）仍計為 held。"""
        uart = "/dev/ttyS0"
        uart_rdev = (4 << 8) | 64
        proc_root.set_path(uart, (HOST_DEV, 11), rdev=uart_rdev)
        proc_root.add_fd(4413, 3, uart, (CONTAINER_DEV_A, 55), rdev=uart_rdev)

        held = self._held(str(proc_root.root))

        assert held is not None
        assert uart in held

    def test_non_dev_link_is_not_stat_resolved(self, proc_root: FakeProcNs) -> None:
        """效能（#R2 reaper）：非 /dev/ 的 link（pipe:／socket:）只做 readlink，不得對其 fd 做 stat。
        此 fd 的 stat 若被呼叫即拋 RuntimeError（非 OSError，會穿出），確保 stat 未被呼叫。"""
        proc_root.add_fd(4414, 4, "pipe:[12345]", RuntimeError("stat must not be called for non-/dev links"))

        held = self._held(str(proc_root.root))

        assert held is not None
        assert "pipe:[12345]" not in held

    def test_missing_proc_returns_none(self, tmp_path: Path) -> None:
        assert self._held(str(tmp_path / "no-such-proc")) is None


class TestDetectMultiOpenNamespace:
    """detect_multi_open 的 tty 持有者比對與 probe 同源（#R2）。"""

    def _detect(self, proc: str):
        from sw_core.multi_open import detect_multi_open

        return detect_multi_open(proc_root=proc, tty_paths=[TTY])

    def test_foreign_namespace_same_string_not_reported(self, proc_root: FakeProcNs) -> None:
        proc_root.add_proc(7001, cmdline="python\0serialwrapd.py\0")
        proc_root.add_fd(7001, 5, TTY, (CONTAINER_DEV_B, 10), rdev=PTS7_RDEV)

        res = self._detect(str(proc_root.root))

        assert res["multi_open"] is False
        assert res["holders"] == {}

    def test_same_inode_reported_under_caller_path(self, proc_root: FakeProcNs) -> None:
        proc_root.add_proc(7002, cmdline="python\0serialwrapd.py\0")
        proc_root.add_fd(7002, 5, TTY, (HOST_DEV, 10), rdev=PTS7_RDEV)

        res = self._detect(str(proc_root.root))

        assert res["holders"] == {TTY: 7002}
        assert res["holders_status"] == "ok"

    def test_non_devpts_mknod_alias_reported_under_caller_path(self, proc_root: FakeProcNs) -> None:
        """Codex P1／#R2：multi_open 亦須接受非 devpts 字元裝置的 mknod 別名，holders 鍵為呼叫端給的 tty 路徑。"""
        from sw_core.multi_open import detect_multi_open

        uart = "/dev/ttyS0"
        uart_rdev = (4 << 8) | 64
        proc_root.set_path(uart, (HOST_DEV, 11), rdev=uart_rdev)
        proc_root.add_proc(7003, cmdline="python\0serialwrapd.py\0")
        proc_root.add_fd(7003, 5, uart, (CONTAINER_DEV_A, 55), rdev=uart_rdev)

        res = detect_multi_open(proc_root=str(proc_root.root), tty_paths=[uart])

        assert res["holders"] == {uart: 7003}
