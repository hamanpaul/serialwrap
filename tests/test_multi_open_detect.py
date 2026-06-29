"""同機多開（two-reader）被動偵測測試（#101）。

涵蓋：
- module-level `detect_multi_open`：fake `/proc` 佈多個 serialwrapd / tty holder。
- 權限降級（讀不到 fd → holders_status='permission'，仍標 multi_open）。
- doctor `_check_single_daemon`。
- daemon status（health()）多開欄位。
"""

import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path


def _make_fake_proc(proc_root: Path, spec: dict) -> None:
    """依 spec 佈置 fake /proc：{pid: {"cmdline": str, "fd": {fd: target}}}。"""
    proc_root.mkdir(parents=True, exist_ok=True)
    for pid, info in spec.items():
        pdir = proc_root / str(pid)
        pdir.mkdir()
        (pdir / "cmdline").write_bytes(info.get("cmdline", "").encode())
        fd_dir = pdir / "fd"
        fd_dir.mkdir()
        for fd, target in info.get("fd", {}).items():
            (fd_dir / str(fd)).symlink_to(target)


class TestDetectMultiOpen(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proc = Path(self._tmp.name) / "proc"

    def test_detect_two_serialwrapd(self) -> None:
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(
            self.proc,
            {
                "2114": {
                    "cmdline": "python\0serialwrapd.py\0--socket\0/run/serialwrap/serialwrapd.sock\0",
                    "fd": {"5": "/dev/ttyUSB0"},
                },
                "9001": {"cmdline": "python\0serialwrapd.py\0", "fd": {}},
                # 非 serialwrapd 程序，不應計入
                "3000": {"cmdline": "bash\0", "fd": {}},
            },
        )
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=["/dev/ttyUSB0"])
        self.assertTrue(res["multi_open"])
        self.assertEqual({d["pid"] for d in res["daemons"]}, {2114, 9001})
        self.assertEqual(res["holders"]["/dev/ttyUSB0"], 2114)
        self.assertEqual(res["holders_status"], "ok")

    def test_single_daemon_ok(self) -> None:
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(self.proc, {"2114": {"cmdline": "python\0serialwrapd.py\0", "fd": {}}})
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=[])
        self.assertFalse(res["multi_open"])
        self.assertEqual([d["pid"] for d in res["daemons"]], [2114])

    def test_no_daemons(self) -> None:
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(self.proc, {"3000": {"cmdline": "bash\0", "fd": {}}})
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=[])
        self.assertFalse(res["multi_open"])
        self.assertEqual(res["daemons"], [])

    def test_missing_proc_root_degrades_unknown(self) -> None:
        from sw_core.multi_open import detect_multi_open

        res = detect_multi_open(proc_root=str(self.proc / "does-not-exist"), tty_paths=[])
        self.assertFalse(res["multi_open"])
        self.assertEqual(res["holders_status"], "unknown")

    def test_permission_denied_degrades(self) -> None:
        from sw_core import multi_open as mo_mod

        _make_fake_proc(
            self.proc,
            {
                "2114": {"cmdline": "python\0serialwrapd.py\0", "fd": {"5": "/dev/ttyUSB0"}},
                "9001": {"cmdline": "python\0serialwrapd.py\0", "fd": {"6": "/dev/ttyUSB1"}},
            },
        )
        real_listdir = os.listdir

        def _fake_listdir(path):
            # 模擬跨 uid 讀不到某 daemon 的 fd 目錄
            if str(path).endswith("/2114/fd"):
                raise PermissionError(13, "Permission denied")
            return real_listdir(path)

        with mock.patch.object(mo_mod.os, "listdir", side_effect=_fake_listdir):
            res = mo_mod.detect_multi_open(
                proc_root=str(self.proc), tty_paths=["/dev/ttyUSB0", "/dev/ttyUSB1"]
            )
        # 仍能確認「另有 serialwrapd 存在」
        self.assertTrue(res["multi_open"])
        self.assertEqual({d["pid"] for d in res["daemons"]}, {2114, 9001})
        # 至少一個 daemon 的 fd 讀不到 → 降級
        self.assertEqual(res["holders_status"], "permission")


class TestDoctorSingleDaemon(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proc = Path(self._tmp.name) / "proc"

    def test_single_daemon_check_fails_on_multi_open(self) -> None:
        from sw_core.doctor_cmd import _check_single_daemon

        _make_fake_proc(
            self.proc,
            {
                "1": {"cmdline": "serialwrapd.py\0", "fd": {}},
                "2": {"cmdline": "serialwrapd.py\0", "fd": {}},
            },
        )
        r = _check_single_daemon(proc_root=str(self.proc))
        self.assertEqual(r["check"], "single_daemon")
        self.assertFalse(r["ok"])
        self.assertTrue(r["detail"])
        self.assertTrue(r["fix"])

    def test_single_daemon_check_ok_on_one(self) -> None:
        from sw_core.doctor_cmd import _check_single_daemon

        _make_fake_proc(self.proc, {"1": {"cmdline": "serialwrapd.py\0", "fd": {}}})
        r = _check_single_daemon(proc_root=str(self.proc))
        self.assertEqual(r["check"], "single_daemon")
        self.assertTrue(r["ok"])
        self.assertEqual(r["fix"], "")

    def test_run_doctor_includes_single_daemon(self) -> None:
        from sw_core.doctor_cmd import run_doctor

        checks = {c["check"] for c in run_doctor()}
        self.assertIn("single_daemon", checks)


if __name__ == "__main__":
    unittest.main()
