"""tests/test_doctor_endpoint_reachable.py — doctor `endpoint_reachable` 檢查（#173）。

涵蓋三態：
- 無 serialwrapd 行程在跑 → advisory ok（on-demand 模式正常）。
- 有行程在跑，本 client 解析到的 endpoint 可連 → ok。
- 有行程在跑，本 client 解析到的 endpoint 連不上（daemon 綁在別的 socket）→ not ok，
  detail 同時點出本 client 解析到的路徑（含來源）與實際 daemon 綁定的路徑。

/proc 掃描沿用 `multi_open.detect_multi_open` 既有的可注入 fake `/proc` 慣例
（`_make_fake_proc`，鏡射 tests/test_multi_open_detect.py）。endpoint 解析與可連性探測
透過 monkeypatch `sw_core.cli._resolve_default_endpoint_with_source` /
`sw_core.cli._endpoint_alive` 直接控制，不依賴真實 socket 檔案存在與否。
"""
from __future__ import annotations

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from sw_core.doctor_cmd import _check_endpoint_reachable


def _make_fake_proc(proc_root: Path, spec: dict) -> None:
    """依 spec 佈置 fake /proc：{pid: {"cmdline": str, "fd": {}}}（鏡射 test_multi_open_detect）。"""
    proc_root.mkdir(parents=True, exist_ok=True)
    for pid, info in spec.items():
        pdir = proc_root / str(pid)
        pdir.mkdir()
        (pdir / "cmdline").write_bytes(info.get("cmdline", "").encode())
        fd_dir = pdir / "fd"
        fd_dir.mkdir()


class TestCheckEndpointReachable(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proc = Path(self._tmp.name) / "proc"

    def test_no_daemon_process_is_advisory_ok(self) -> None:
        """無 serialwrapd 行程 → ok=True（on-demand 模式尚未啟動不是異常）。"""
        _make_fake_proc(self.proc, {"3000": {"cmdline": "bash\0"}})
        with mock.patch(
            "sw_core.cli._resolve_default_endpoint_with_source",
            return_value=("/run/user/1000/serialwrap/serialwrapd.sock", "預設"),
        ):
            result = _check_endpoint_reachable(proc_root=str(self.proc))
        self.assertEqual(result["check"], "endpoint_reachable")
        self.assertTrue(result["ok"])
        self.assertIn("未偵測到執行中的 serialwrapd", result["detail"])

    def test_daemon_running_and_endpoint_reachable_is_ok(self) -> None:
        """有行程在跑，本 client 解析到的 endpoint 可連 → ok=True。"""
        _make_fake_proc(
            self.proc,
            {"2114": {"cmdline": "python\0serialwrapd.py\0--socket\0/tmp/serialwrap/serialwrapd.sock\0"}},
        )
        with (
            mock.patch(
                "sw_core.cli._resolve_default_endpoint_with_source",
                return_value=("/tmp/serialwrap/serialwrapd.sock", "config.yaml"),
            ),
            mock.patch("sw_core.cli._endpoint_alive", return_value=True) as m_alive,
        ):
            result = _check_endpoint_reachable(proc_root=str(self.proc))
        m_alive.assert_called_once_with("/tmp/serialwrap/serialwrapd.sock")
        self.assertTrue(result["ok"])
        self.assertIn("/tmp/serialwrap/serialwrapd.sock", result["detail"])
        self.assertIn("config.yaml", result["detail"])

    def test_daemon_running_but_endpoint_unreachable_is_not_ok(self) -> None:
        """有行程在跑，但本 client 解析到的 endpoint 連不上（daemon 實際綁在別處）→ not ok。

        detail 必須同時點出本 client 解析到的路徑（含來源）與實際 daemon 綁定的路徑——
        這是 #173 的核心驗收：把事故從「一個下午」縮短到「一分鐘」。
        """
        _make_fake_proc(
            self.proc,
            {"2114": {"cmdline": "python\0serialwrapd.py\0--socket\0/tmp/serialwrap/serialwrapd.sock\0"}},
        )
        with (
            mock.patch(
                "sw_core.cli._resolve_default_endpoint_with_source",
                return_value=("/run/user/1000/serialwrap/serialwrapd.sock", "預設（SOCKET_PATH，受 SERIALWRAP_STATE_DIR/XDG 環境變數影響）"),
            ),
            mock.patch("sw_core.cli._endpoint_alive", return_value=False),
        ):
            result = _check_endpoint_reachable(proc_root=str(self.proc))
        self.assertFalse(result["ok"])
        self.assertIn("/run/user/1000/serialwrap/serialwrapd.sock", result["detail"])
        self.assertIn("預設", result["detail"])
        self.assertIn("/tmp/serialwrap/serialwrapd.sock", result["detail"])
        self.assertTrue(result["fix"])

    def test_daemon_socket_unknown_still_shown_in_detail(self) -> None:
        """行程 --socket 擷取不到（舊版 daemon／非典型 argv）時，detail 仍需標示『未知』而非空白。"""
        _make_fake_proc(self.proc, {"2114": {"cmdline": "python\0serialwrapd.py\0"}})
        with (
            mock.patch(
                "sw_core.cli._resolve_default_endpoint_with_source",
                return_value=("/run/user/1000/serialwrap/serialwrapd.sock", "預設"),
            ),
            mock.patch("sw_core.cli._endpoint_alive", return_value=False),
        ):
            result = _check_endpoint_reachable(proc_root=str(self.proc))
        self.assertFalse(result["ok"])
        self.assertIn("未知", result["detail"])


if __name__ == "__main__":
    unittest.main()
