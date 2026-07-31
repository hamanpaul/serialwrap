"""#157：file push/pull 的 chunk timeout 推導與 chunk_size 參數線單元測試。

覆蓋三層「值有沒有正確流下來」的純邏輯行為（真機截斷/逾時留給 realhw regression F7）：
1. SessionManager 層——未帶 chunk_timeout_s 時沿用 profile.timeout_s（夾地板）、
   顯式帶時覆寫、chunk_size 預設為 DEFAULT_CHUNK_SIZE（非殘留 2048）。
2. SerialwrapService RPC 層——chunk_size/chunk_timeout_s 解析、預設與 0 防呆。
3. CLI argparse 層——--chunk-size 預設、--chunk-timeout 解析與 RPC params 組裝
   （不帶時不佔 payload 欄位）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sw_core.session_manager as sm_mod
from sw_core.cli import build_parser, main
from sw_core.config import SessionProfile, UartProfile
from sw_core.file_transfer import DEFAULT_CHUNK_SIZE
from sw_core.session_manager import (
    _MIN_FILE_CHUNK_TIMEOUT_S,
    _MIN_FILE_PULL_TIMEOUT_S,
    SessionManager,
)
from sw_core.wal import WalWriter

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


def _make_profile(timeout_s: float = 10.0) -> SessionProfile:
    return SessionProfile(
        profile_name="p",
        com="COM0",
        act_no=1,
        alias="lab+1",
        device_by_id="/dev/serial/by-id/orig",
        platform="prpl",
        timeout_s=timeout_s,
        uart=UartProfile(),
    )


class _ManagerMixin(unittest.TestCase):
    """建最小 SessionManager + READY session（bridge 為 MagicMock）。"""

    def setUp(self) -> None:
        self._td = state_iso.isolate_testcase(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _make_mgr_ready(self, timeout_s: float) -> tuple[SessionManager, mock.MagicMock]:
        profile = _make_profile(timeout_s=timeout_s)
        mgr = SessionManager(
            [profile],
            WalWriter(wal_dir=self._tmp.name),
            on_ready=lambda _sid: None,
            on_detached=lambda _sid: None,
        )
        session = mgr.get_session("COM0")
        assert session is not None
        session.bridge = mock.MagicMock()
        session.state = "READY"
        return mgr, session.bridge


class TestFilePushChunkTimeout(_ManagerMixin):
    """file_push：timeout 推導（profile 沿用／地板／顯式覆寫）與 chunk_size 預設。"""

    def _push(self, mgr: SessionManager, **kwargs) -> mock.Mock:
        with mock.patch("sw_core.file_transfer.push_file",
                        return_value={"ok": True}) as m:
            resp = mgr.file_push("COM0", local_path="/tmp/src", remote_path="/tmp/dst",
                                 **kwargs)
        self.assertTrue(resp["ok"])
        m.assert_called_once()
        return m

    def test_default_profile_timeout_matches_legacy(self) -> None:
        """prpl 預設 timeout_s=10.0、未帶 chunk_timeout_s → 10.0（與修復前一致，不回歸）。"""
        mgr, _ = self._make_mgr_ready(timeout_s=10.0)
        m = self._push(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 10.0)

    def test_larger_profile_timeout_now_effective(self) -> None:
        """bcm 類調大 timeout_s=20.0 → 20.0（修復前被寫死 10.0 吃掉，修復核心證明）。"""
        mgr, _ = self._make_mgr_ready(timeout_s=20.0)
        m = self._push(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 20.0)

    def test_explicit_chunk_timeout_overrides(self) -> None:
        """顯式 chunk_timeout_s=5.0 優先於 profile.timeout_s=20.0。"""
        mgr, _ = self._make_mgr_ready(timeout_s=20.0)
        m = self._push(mgr, chunk_timeout_s=5.0)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 5.0)

    def test_floor_applies_on_tiny_profile_timeout(self) -> None:
        """profile.timeout_s=1.0 極端低 → 地板 _MIN_FILE_CHUNK_TIMEOUT_S=5.0 生效。"""
        self.assertEqual(_MIN_FILE_CHUNK_TIMEOUT_S, 5.0)
        mgr, _ = self._make_mgr_ready(timeout_s=1.0)
        m = self._push(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 5.0)

    def test_default_chunk_size_forwarded(self) -> None:
        """未帶 chunk_size → 轉呼 push_file 收到 DEFAULT_CHUNK_SIZE（防手滑寫回 2048）。"""
        mgr, _ = self._make_mgr_ready(timeout_s=10.0)
        m = self._push(mgr)
        self.assertEqual(m.call_args.kwargs["chunk_size"], DEFAULT_CHUNK_SIZE)

    def test_ack_mode_default_auto_forwarded(self) -> None:
        """#161：未帶 ack_mode → push_file 收到 "auto"。"""
        mgr, _ = self._make_mgr_ready(timeout_s=10.0)
        m = self._push(mgr)
        self.assertEqual(m.call_args.kwargs["ack_mode"], "auto")

    def test_ack_mode_explicit_forwarded(self) -> None:
        """#161：顯式 ack_mode="none" 原樣透傳。"""
        mgr, _ = self._make_mgr_ready(timeout_s=10.0)
        m = self._push(mgr, ack_mode="none")
        self.assertEqual(m.call_args.kwargs["ack_mode"], "none")


class TestFilePullChunkTimeout(_ManagerMixin):
    """file_pull：timeout 推導平行四案（地板換 _MIN_FILE_PULL_TIMEOUT_S=30.0）。"""

    def _pull(self, mgr: SessionManager, **kwargs) -> mock.Mock:
        with mock.patch("sw_core.file_transfer.pull_file",
                        return_value={"ok": True}) as m:
            resp = mgr.file_pull("COM0", remote_path="/tmp/src", **kwargs)
        self.assertTrue(resp["ok"])
        m.assert_called_once()
        return m

    def test_default_profile_timeout_floored_to_30(self) -> None:
        """prpl 預設 timeout_s=10.0 → max(10.0, 30.0)=30.0（與修復前一致，不回歸）。"""
        self.assertEqual(_MIN_FILE_PULL_TIMEOUT_S, 30.0)
        mgr, _ = self._make_mgr_ready(timeout_s=10.0)
        m = self._pull(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 30.0)

    def test_larger_profile_timeout_now_effective(self) -> None:
        """timeout_s=45.0 超過地板 → 45.0（修復前被寫死 30.0 吃掉）。"""
        mgr, _ = self._make_mgr_ready(timeout_s=45.0)
        m = self._pull(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 45.0)

    def test_explicit_chunk_timeout_overrides(self) -> None:
        mgr, _ = self._make_mgr_ready(timeout_s=45.0)
        m = self._pull(mgr, chunk_timeout_s=7.5)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 7.5)

    def test_floor_applies_on_tiny_profile_timeout(self) -> None:
        mgr, _ = self._make_mgr_ready(timeout_s=1.0)
        m = self._pull(mgr)
        self.assertEqual(m.call_args.kwargs["timeout_s"], 30.0)


class TestServiceRpcParams(unittest.TestCase):
    """SerialwrapService RPC 層：chunk_size／chunk_timeout_s 解析與防呆。"""

    def setUp(self) -> None:
        state_iso.isolate_testcase(self)
        from sw_core.service import SerialwrapService

        self.svc = SerialwrapService([])
        # #139 起 _resolve_session_id 增 keyword-only `source` 參數，stub 需吸收。
        self.svc._resolve_session_id = lambda _sel, **_kw: ("p:COM0", None)

    def _rpc_push(self, params: dict) -> mock.Mock:
        m = mock.Mock(return_value={"ok": True})
        self.svc._sessions.file_push = m
        base = {"selector": "COM0", "local_path": "/tmp/a", "remote_path": "/tmp/b"}
        resp = self.svc.rpc("file.push", {**base, **params})
        self.assertTrue(resp["ok"])
        m.assert_called_once()
        return m

    def test_push_invalid_chunk_timeout_returns_invalid_args(self) -> None:
        """Copilot review：非數字 chunk_timeout_s 不得讓 ValueError 穿越 RPC 邊界。"""
        base = {"selector": "COM0", "local_path": "/tmp/a", "remote_path": "/tmp/b"}
        resp = self.svc.rpc("file.push", {**base, "chunk_timeout_s": "abc"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_push_invalid_chunk_size_returns_invalid_args(self) -> None:
        base = {"selector": "COM0", "local_path": "/tmp/a", "remote_path": "/tmp/b"}
        resp = self.svc.rpc("file.push", {**base, "chunk_size": "xyz"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_pull_invalid_chunk_timeout_returns_invalid_args(self) -> None:
        resp = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/b",
                                          "chunk_timeout_s": ""})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_push_defaults(self) -> None:
        """不帶 chunk_size/chunk_timeout_s → DEFAULT_CHUNK_SIZE／None。"""
        m = self._rpc_push({})
        self.assertEqual(m.call_args.kwargs["chunk_size"], DEFAULT_CHUNK_SIZE)
        self.assertIsNone(m.call_args.kwargs["chunk_timeout_s"])

    def test_push_chunk_size_zero_clamped(self) -> None:
        """chunk_size=0 不得讓 ValueError 穿越 RPC 邊界——clamp 成 1。"""
        m = self._rpc_push({"chunk_size": 0})
        self.assertEqual(m.call_args.kwargs["chunk_size"], DEFAULT_CHUNK_SIZE)  # 0 falsy → 預設
        m2 = self._rpc_push({"chunk_size": -8})
        self.assertEqual(m2.call_args.kwargs["chunk_size"], 1)  # 負值 → clamp 1

    def test_push_chunk_timeout_forwarded(self) -> None:
        m = self._rpc_push({"chunk_timeout_s": 42.5})
        self.assertEqual(m.call_args.kwargs["chunk_timeout_s"], 42.5)

    def test_pull_defaults_and_forwarding(self) -> None:
        m = mock.Mock(return_value={"ok": True})
        self.svc._sessions.file_pull = m
        resp = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/a"})
        self.assertTrue(resp["ok"])
        self.assertIsNone(m.call_args.kwargs["chunk_timeout_s"])
        m.reset_mock()
        resp = self.svc.rpc(
            "file.pull",
            {"selector": "COM0", "remote_path": "/tmp/a", "chunk_timeout_s": 12.5})
        self.assertTrue(resp["ok"])
        self.assertEqual(m.call_args.kwargs["chunk_timeout_s"], 12.5)

    def test_push_ack_mode_whitelist_rejects_unknown(self) -> None:
        """#161：file.push ack_mode 非白名單（auto/echo/none）→ INVALID_ARGS。"""
        base = {"selector": "COM0", "local_path": "/tmp/a", "remote_path": "/tmp/b"}
        resp = self.svc.rpc("file.push", {**base, "ack_mode": "bogus"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_push_ack_mode_default_and_forwarding(self) -> None:
        """#161：不帶 ack_mode → "auto"；顯式合法值原樣透傳。"""
        m = self._rpc_push({})
        self.assertEqual(m.call_args.kwargs["ack_mode"], "auto")
        for mode in ("auto", "echo", "none"):
            m = self._rpc_push({"ack_mode": mode})
            self.assertEqual(m.call_args.kwargs["ack_mode"], mode)

    def test_pull_ack_mode_whitelist_rejects_unknown(self) -> None:
        """#161：file.pull 同樣驗 ack_mode 白名單（介面對齊；合法值不影響行為）。"""
        resp = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/b",
                                          "ack_mode": "bogus"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error_code"], "INVALID_ARGS")

    def test_pull_ack_mode_valid_accepted(self) -> None:
        """#161：pull 帶合法 ack_mode 仍正常執行（驗證不轉傳、不炸）。"""
        m = mock.Mock(return_value={"ok": True})
        self.svc._sessions.file_pull = m
        resp = self.svc.rpc("file.pull", {"selector": "COM0", "remote_path": "/tmp/a",
                                          "ack_mode": "echo"})
        self.assertTrue(resp["ok"])
        self.assertNotIn("ack_mode", m.call_args.kwargs)


class TestCliArgsAndParams(unittest.TestCase):
    """CLI argparse 層：--chunk-size 預設、--chunk-timeout 解析與 RPC params 組裝。"""

    _PUSH_ARGV = ["file", "push", "--selector", "COM0",
                  "--local", "/tmp/a", "--remote", "/tmp/b"]
    _PULL_ARGV = ["file", "pull", "--selector", "COM0", "--remote", "/tmp/a"]

    def test_push_defaults(self) -> None:
        args = build_parser().parse_args(self._PUSH_ARGV)
        self.assertEqual(args.chunk_size, DEFAULT_CHUNK_SIZE)
        self.assertIsNone(args.chunk_timeout_s)

    def test_push_chunk_timeout_parsed(self) -> None:
        args = build_parser().parse_args([*self._PUSH_ARGV, "--chunk-timeout", "12.5"])
        self.assertEqual(args.chunk_timeout_s, 12.5)

    def test_pull_chunk_timeout_parsed(self) -> None:
        args = build_parser().parse_args([*self._PULL_ARGV, "--chunk-timeout", "9.0"])
        self.assertEqual(args.chunk_timeout_s, 9.0)

    def _dispatch(self, argv: list[str]) -> tuple[str, dict]:
        with mock.patch("sw_core.cli._run_rpc", return_value=0) as m:
            rc = main(argv)
        self.assertEqual(rc, 0)
        m.assert_called_once()
        _, method, params = m.call_args.args
        return method, params

    def test_push_params_omit_chunk_timeout_by_default(self) -> None:
        """不帶 --chunk-timeout → RPC params 不含該 key（daemon 走 profile 推導）。"""
        method, params = self._dispatch(self._PUSH_ARGV)
        self.assertEqual(method, "file.push")
        self.assertEqual(params["chunk_size"], DEFAULT_CHUNK_SIZE)
        self.assertNotIn("chunk_timeout_s", params)

    def test_push_params_include_explicit_chunk_timeout(self) -> None:
        method, params = self._dispatch([*self._PUSH_ARGV, "--chunk-timeout", "12.5"])
        self.assertEqual(method, "file.push")
        self.assertEqual(params["chunk_timeout_s"], 12.5)

    def test_pull_params_include_explicit_chunk_timeout(self) -> None:
        method, params = self._dispatch([*self._PULL_ARGV, "--chunk-timeout", "9.0"])
        self.assertEqual(method, "file.pull")
        self.assertEqual(params["chunk_timeout_s"], 9.0)

    def test_pull_params_omit_chunk_timeout_by_default(self) -> None:
        method, params = self._dispatch(self._PULL_ARGV)
        self.assertEqual(method, "file.pull")
        self.assertNotIn("chunk_timeout_s", params)

    def test_ack_mode_default_auto(self) -> None:
        """#161：--ack-mode 預設 auto，push/pull 皆組進 params。"""
        args = build_parser().parse_args(self._PUSH_ARGV)
        self.assertEqual(args.ack_mode, "auto")
        method, params = self._dispatch(self._PUSH_ARGV)
        self.assertEqual(params["ack_mode"], "auto")
        method, params = self._dispatch(self._PULL_ARGV)
        self.assertEqual(params["ack_mode"], "auto")

    def test_ack_mode_explicit_parsed_and_forwarded(self) -> None:
        """#161：--ack-mode none/echo 解析並組進 RPC params。"""
        method, params = self._dispatch([*self._PUSH_ARGV, "--ack-mode", "none"])
        self.assertEqual(method, "file.push")
        self.assertEqual(params["ack_mode"], "none")
        method, params = self._dispatch([*self._PULL_ARGV, "--ack-mode", "echo"])
        self.assertEqual(method, "file.pull")
        self.assertEqual(params["ack_mode"], "echo")

    def test_ack_mode_invalid_choice_rejected(self) -> None:
        """#161：非白名單值被 argparse choices 擋在 CLI 層（SystemExit 2）。"""
        with self.assertRaises(SystemExit):
            build_parser().parse_args([*self._PUSH_ARGV, "--ack-mode", "bogus"])


if __name__ == "__main__":
    unittest.main()
