from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sw_core.session_manager as sm_mod
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


class StatResultWithRdev:
    """具備 st_rdev 的 stat 結果物件。"""

    def __init__(self, rdev: int = 12345, mode: int = 0o20666) -> None:
        self.st_rdev = rdev
        self.st_mode = mode


class StatResultWithoutRdev:
    """缺乏 st_rdev 的 stat 結果物件（模擬 Windows 或非 POSIX 環境）。"""

    def __init__(self, mode: int = 0o20666) -> None:
        self.st_mode = mode


class StatIdentity:
    """以 (st_dev, st_ino) 描述 inode 身分的 stat 替身（#R2：holder 判定依此，不依 st_rdev）。"""

    def __init__(self, dev: int, ino: int, rdev: int | None = None, mode: int = 0o20666) -> None:
        self.st_dev = dev
        self.st_ino = ino
        self.st_mode = mode
        if rdev is not None:
            self.st_rdev = rdev


def _probe_posix_branch(mgr: SessionManager, path: str, proc: str) -> dict:
    """以 POSIX procfs 分支呼叫 _probe_external_holder。

    `probe_holders` 在 os.name == "nt" 時直接回 unsupported、不掃描 proc 目錄。本檔要驗證的是
    procfs 比對邏輯，假 /proc 與 stat 替身皆為暫存檔或記憶體物件，不需真實 /proc；因此於呼叫期間
    釘住 os.name，讓 Linux 與 Windows 宿主都走同一條分支。只影響本次呼叫，不 skip、不改斷言。
    """
    with mock.patch.object(os, "name", "posix"):
        return mgr._probe_external_holder(path, _proc_root=proc)


class TestHolderProbePortability(unittest.TestCase):
    """#199 缺 st_rdev 之 _probe_external_holder 跨平台容錯測試。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))

    def _mgr(self) -> SessionManager:
        return SessionManager(
            [],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _s: None,
            on_detached=lambda _s: None,
        )

    def test_missing_endpoint_st_rdev_tolerated(self) -> None:
        """當 endpoint stat 缺乏 st_rdev 時，不拋 AttributeError，且同一 inode 仍能正確匹配（#R2：身分改以 st_dev／st_ino）。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / "1234" / "fd" / "3")

        with mock.patch("sw_core.session_manager.os.stat") as mock_stat:
            mock_stat.return_value = StatIdentity(dev=60, ino=17)
            res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))

        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_missing_fd_st_rdev_tolerated(self) -> None:
        """當 endpoint 具備有效 st_rdev 但 fd stat 缺乏 st_rdev 且 path 不同時，不拋 AttributeError 且不誤判。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/serial/by-path/unrelated", proc / "1234" / "fd" / "3")

        def _stat_side_effect(path: str, *args, **kwargs):
            if path == "/dev/ttyUSB0":
                return StatResultWithRdev(rdev=42)
            return StatResultWithoutRdev()

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))

        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])

    def test_alias_path_same_inode_matched(self) -> None:
        """fd 經由不同路徑（by-id 別名）指向同一 inode 時仍是 holder（#199 別名正面案例，改以 inode 身分判定）。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/serial/by-id/usb-device-part", proc / "1234" / "fd" / "3")

        def _stat_side_effect(path: str, *args, **kwargs):
            return StatIdentity(dev=60, ino=17, rdev=500)

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))

        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_posix_char_device_rdev_matching(self) -> None:
        """非 devpts 字元裝置：fd 經由不同 mknod 節點（st_dev／st_ino 皆不同）但 st_rdev 相同（major 4，非 136–143）
        仍為同一實體 UART 的 holder（#R2／Codex P1：容器以 mknod 掛入同一 UART 節點）。
        此 nodeid 為凍結清單既有項（l1_required.lock.json），名稱與語意保持一致，不得改名。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyS0", proc / "1234" / "fd" / "3")
        fd_path = str(proc / "1234" / "fd" / "3")
        rdev = (4 << 8) | 64  # major 4（ttyS），非 devpts

        def _stat_side_effect(path: str, *args, **kwargs):
            if path == fd_path:
                return StatIdentity(dev=140, ino=99, rdev=rdev)
            return StatIdentity(dev=60, ino=17, rdev=rdev)

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/ttyS0", str(proc))

        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_same_rdev_different_devpts_instance_not_matched(self) -> None:
        """#R2 回歸：st_rdev 相同（同為 136:N）但 st_dev 不同（另一 devpts instance，例如 docker 容器內的
        /dev/pts/N）不得判為 holder；改用 rdev 會在此誤判，導致 attach 回 DeviceHeldError。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/pts/7", proc / "1234" / "fd" / "3")

        def _stat_side_effect(path: str, *args, **kwargs):
            if path == "/dev/pts/7":
                return StatIdentity(dev=60, ino=10, rdev=34823)
            return StatIdentity(dev=148, ino=10, rdev=34823)

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/pts/7", str(proc))

        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])
        self.assertFalse(res["holder_unknown"])

    def test_posix_char_device_different_rdev_not_matched(self) -> None:
        """當 path 不同且有效 st_rdev 亦不同時，不誤判為 holder。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB1", proc / "1234" / "fd" / "3")

        def _stat_side_effect(path: str, *args, **kwargs):
            if path == "/dev/ttyUSB0":
                return StatResultWithRdev(rdev=500)
            return StatResultWithRdev(rdev=501)

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))

        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])

    def test_zero_rdev_not_used_as_identity_evidence(self) -> None:
        """st_rdev 為 0 時不作為同一性證據，path 不同時不得因雙方 rdev 為 0 而誤判。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/different_device", proc / "1234" / "fd" / "3")

        def _stat_side_effect(path: str, *args, **kwargs):
            return StatResultWithRdev(rdev=0)

        with mock.patch("sw_core.session_manager.os.stat", side_effect=_stat_side_effect):
            res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))

        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])

    def test_same_path_matched(self) -> None:
        """同一實體 tty（同 inode）仍能正確匹配 holder（#R2：以 inode 身分判定，不以路徑字串）。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        dev = Path(self._tmp.name) / "dev" / "ttyUSB0"
        dev.parent.mkdir(parents=True)
        dev.write_bytes(b"")
        os.symlink(dev, proc / "1234" / "fd" / "3")

        res = _probe_posix_branch(mgr, str(dev), str(proc))

        self.assertEqual(res["pids"], [1234])
        self.assertEqual(res["holder"], 1234)

    def test_nt_branch_reports_holder_unknown(self) -> None:
        """os.name == "nt" 時 probe_holders 不掃 procfs，回 holder_unknown（不得誤報為「無 holder」）。"""
        mgr = self._mgr()
        proc = Path(self._tmp.name) / "proc"
        (proc / "1234" / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / "1234" / "fd" / "3")

        with mock.patch.object(os, "name", "nt"):
            res = mgr._probe_external_holder("/dev/ttyUSB0", _proc_root=str(proc))

        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])
        self.assertTrue(res["holder_unknown"])

    def test_no_proc_returns_empty_safely(self) -> None:
        """當 _proc_root 不可讀時，不拋出例外且明確標記 holder 未知。"""
        mgr = self._mgr()
        nonexistent_proc = Path(self._tmp.name) / "nonexistent_proc"
        res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(nonexistent_proc))
        self.assertEqual(res, {"pids": [], "holder": None, "holder_unknown": True})

    def test_my_pid_ignored(self) -> None:
        """自身 pid 在 _proc_root 中應被忽略，不計入 external holders。"""
        mgr = self._mgr()
        my_pid = os.getpid()
        proc = Path(self._tmp.name) / "proc"
        (proc / str(my_pid) / "fd").mkdir(parents=True)
        os.symlink("/dev/ttyUSB0", proc / str(my_pid) / "fd" / "3")

        res = _probe_posix_branch(mgr, "/dev/ttyUSB0", str(proc))
        self.assertEqual(res["pids"], [])
        self.assertIsNone(res["holder"])
