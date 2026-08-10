"""#172：CLI 失敗時 stderr 不得丟棄 message／hint。

涵蓋兩條驗收路徑：
1. ``_run_rpc``：原本 ``error_code or message`` 短路讓有 ``error_code`` 時
   ``message`` 永遠看不到——對不存在的 socket 下 ``session list``，
   ``SOCKET_ERROR`` 的 ``message`` 正是 ``str(OSError)``（errno + socket 路徑），
   必須同時出現在 stderr。
2. 非 RPC 錯誤出口（``daemon start`` 於 systemd-system 模式重導 ``service start``
   失敗，例如未帶 ``--with-sudo`` 觸發 ``NEEDS_SUDO``）：exit 非零時 stderr 不得
   為空，須含 ``error_code`` 與 ``hint``。
"""
from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from sw_core import cli


class TestRunRpcStderrKeepsMessage(unittest.TestCase):
    """驗收 1：對不存在的 socket 下 `session list`，stderr 含路徑與 errno。"""

    def test_socket_error_stderr_includes_error_code_and_message(self) -> None:
        socket_path = "/tmp/sw172-no-such-daemon.sock"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--socket", socket_path, "session", "list"])

        self.assertEqual(rc, 2)
        stderr_text = err.getvalue()
        # code 一定露出（相容性關鍵：substring 比對 "failed: SOCKET_ERROR" 不得斷）
        self.assertIn("failed: SOCKET_ERROR", stderr_text)
        # #172 核心：message（含 errno，AF_UNIX ENOENT 的 str(OSError) 未含路徑本身，
        # 由呼叫端 socket_path 提供上下文）不得再被短路丟棄
        self.assertIn("[Errno 2]", stderr_text)
        # stdout 仍是乾淨可解析 JSON（契約不變）
        import json

        parsed = json.loads(out.getvalue())
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error_code"], "SOCKET_ERROR")

    def test_message_only_response_does_not_duplicate(self) -> None:
        """無 error_code 但有 message（既有 #94 fallback）：不得重複印同一句話。"""
        stub_resp = {"ok": False, "message": "boom"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sw_core.cli.rpc_call", return_value=stub_resp):
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["session", "list"])

        self.assertEqual(rc, 2)
        stderr_text = err.getvalue()
        self.assertEqual(stderr_text.count("boom"), 1)
        self.assertIn("failed: boom", stderr_text)


class TestNonRpcExitsMirrorStderr(unittest.TestCase):
    """驗收 2：`daemon start` 重導 `service start`（systemd-system 未帶 --with-sudo）
    失敗時，exit 2 且 stderr 含 NEEDS_SUDO 與 hint。"""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            profile_dir="/tmp/profiles",
            socket="/tmp/serialwrap.sock",
            lock="/tmp/serialwrap.lock",
            foreground=False,
            with_sudo=False,
            endpoint=None,
        )

    def test_daemon_start_needs_sudo_stderr_not_empty(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.mode.return_value = "systemd-system"
        needs_sudo_resp = {
            "ok": False,
            "mode": "systemd-system",
            "action": "start",
            "error_code": "NEEDS_SUDO",
            "hint": "需 root：請執行 `sudo systemctl start serialwrap`（或加 --with-sudo 讓本指令代跑）",
        }
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli.service_action", return_value=dict(needs_sudo_resp)),
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = cli._run_daemon_start(self._args())

        self.assertEqual(rc, 2)
        popen.assert_not_called()
        stderr_text = err.getvalue()
        self.assertNotEqual(stderr_text, "", "非 RPC 錯誤出口的 stderr 不得為空（#172）")
        self.assertIn("NEEDS_SUDO", stderr_text)
        self.assertIn("需 root", stderr_text)  # hint 內容

    def test_daemon_start_needs_sudo_via_main_exit_code_and_stderr(self) -> None:
        """經 `main()` 整條路徑驗證（非直呼內部函式）：exit 2 且 stderr 非空。"""
        fake_rc = mock.Mock()
        fake_rc.mode.return_value = "systemd-system"
        needs_sudo_resp = {
            "ok": False,
            "mode": "systemd-system",
            "action": "start",
            "error_code": "NEEDS_SUDO",
            "hint": "需 root：請執行 `sudo systemctl start serialwrap`（或加 --with-sudo 讓本指令代跑）",
        }
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch("sw_core.cli.service_action", return_value=dict(needs_sudo_resp)),
            mock.patch("sw_core.cli.subprocess.Popen") as popen,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = cli.main(["daemon", "start"])

        self.assertEqual(rc, 2)
        popen.assert_not_called()
        stderr_text = err.getvalue()
        self.assertIn("NEEDS_SUDO", stderr_text)
        self.assertIn("hint", stderr_text.lower())


if __name__ == "__main__":
    unittest.main()
