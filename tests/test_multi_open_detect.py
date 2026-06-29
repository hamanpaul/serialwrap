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

    def test_detect_pipx_console_script_form(self) -> None:
        """#101 實機回歸：pipx/venv console_script 形式（實機 prod daemon）——
        ``python /home/.../bin/serialwrapd --socket ...``，argv1 basename 為
        ``serialwrapd``（無 ``.py``）。曾因 _is_serialwrapd 只認 ``serialwrapd.py``
        而漏掉真正的 prod daemon（實機驗證才現形）。"""
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(
            self.proc,
            {
                "1095518": {
                    # console_script 形式：python 執行 venv 內的 serialwrapd 入口（argv1 basename
                    # 為 serialwrapd、無 .py、為路徑）。用 /opt 中性路徑避免 R-21 個人路徑誤判。
                    "cmdline": "/opt/serialwrap-venv/bin/python\0"
                    "/opt/serialwrap-venv/bin/serialwrapd\0--socket\0/run/serialwrap/serialwrapd.sock\0",
                    "fd": {"7": "/dev/ttyUSB1"},
                },
                "1200888": {"cmdline": "python3\0/worktree/serialwrapd.py\0", "fd": {}},
            },
        )
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=["/dev/ttyUSB1"])
        self.assertTrue(res["multi_open"])
        self.assertEqual({d["pid"] for d in res["daemons"]}, {1095518, 1200888})
        self.assertEqual(res["holders"]["/dev/ttyUSB1"], 1095518)

    def test_argv0_serialwrapd_py_detected(self) -> None:
        """argv0 直接是 serialwrapd.py（直接 exec shim）也要算（回歸：曾把 .py 判定
        限縮到 argv1+ 而漏掉 argv0）。"""
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(
            self.proc,
            {"1": {"cmdline": "serialwrapd.py\0", "fd": {}},
             "2": {"cmdline": "serialwrapd.py\0", "fd": {}}},
        )
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=[])
        self.assertTrue(res["multi_open"])
        self.assertEqual({d["pid"] for d in res["daemons"]}, {1, 2})

    def test_no_daemons(self) -> None:
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(self.proc, {"3000": {"cmdline": "bash\0", "fd": {}}})
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=[])
        self.assertFalse(res["multi_open"])
        self.assertEqual(res["daemons"], [])

    def test_grep_serialwrapd_not_misdetected(self) -> None:
        """#101 5a：`grep serialwrapd` 的命令列僅「字串含 serialwrapd」，argv0 為 grep、
        無任何 arg 的 basename 為 serialwrapd.py，故不應被誤判為 daemon。"""
        from sw_core.multi_open import detect_multi_open

        _make_fake_proc(
            self.proc,
            {
                # 真正的 daemon（薄 shim 路徑）
                "2114": {"cmdline": "python\0/opt/serialwrapd.py\0", "fd": {}},
                # `grep serialwrapd` —— 不應計入
                "4000": {"cmdline": "grep\0serialwrapd\0", "fd": {}},
            },
        )
        res = detect_multi_open(proc_root=str(self.proc), tty_paths=[])
        self.assertEqual({d["pid"] for d in res["daemons"]}, {2114})
        # 只有一個真正 daemon → 非多開
        self.assertFalse(res["multi_open"])

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


class TestDaemonStatusMultiOpen(unittest.TestCase):
    """Task 10：daemon status（health()）回應加多開 / 外部持有者欄位。"""

    def setUp(self) -> None:
        import sw_core.session_manager as sm_mod

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state_path = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(Path(self._tmp.name) / "state.json")
        self.addCleanup(lambda: setattr(sm_mod, "STATE_PATH", self._old_state_path))
        self._old_wal = os.environ.get("SERIALWRAP_WAL_DIR")
        os.environ["SERIALWRAP_WAL_DIR"] = self._tmp.name
        self.addCleanup(self._restore_wal)
        self._byid = Path(self._tmp.name) / "by-id-empty"
        self._byid.mkdir()

    def _restore_wal(self) -> None:
        if self._old_wal is None:
            os.environ.pop("SERIALWRAP_WAL_DIR", None)
        else:
            os.environ["SERIALWRAP_WAL_DIR"] = self._old_wal

    def _make_service(self):
        from sw_core.service import SerialwrapService

        return SerialwrapService(
            [],
            templates=[],
            by_id_dir=str(self._byid),
            by_path_dir=str(Path(self._tmp.name) / "nonexistent-by-path"),
        )

    def test_health_includes_multi_open_fields(self) -> None:
        svc = self._make_service()
        fake = {
            "multi_open": False,
            "daemons": [{"pid": 1}],
            "holders": {},
            "holders_status": "ok",
        }
        with mock.patch("sw_core.service.detect_multi_open", return_value=fake):
            st = svc.health()
        self.assertIn("multi_open", st)
        self.assertFalse(st["multi_open"])
        self.assertEqual(st["foreign_holders"], {})
        self.assertEqual(st["multi_open_detail"]["holders_status"], "ok")
        self.assertEqual(st["multi_open_detail"]["daemons"], [{"pid": 1}])

    def test_health_passes_attached_tty_paths(self) -> None:
        from sw_core.config import SessionProfile, UartProfile
        from sw_core.session_manager import SessionRuntime

        svc = self._make_service()
        # 注入一個 attached session（有 attached_real_path）
        sm = svc._sessions
        profile = SessionProfile(
            profile_name="prpl-template",
            com="COM0",
            act_no=1,
            alias="prpl-template+1",
            device_by_id="/dev/serial/by-id/x",
            platform="prpl",
            uart=UartProfile(),
        )
        rt = SessionRuntime(session_id="prpl-template:COM0", profile=profile)
        rt.state = "READY"
        rt.attached_real_path = "/dev/ttyUSB9"
        with sm._lock:
            sm._sessions["prpl-template:COM0"] = rt

        captured: dict = {}

        def _fake_detect(proc_root="/proc", tty_paths=None):
            captured["tty_paths"] = tty_paths
            return {"multi_open": False, "daemons": [], "holders": {}, "holders_status": "ok"}

        with mock.patch("sw_core.service.detect_multi_open", side_effect=_fake_detect):
            svc.health()
        self.assertIn("/dev/ttyUSB9", captured["tty_paths"])


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
