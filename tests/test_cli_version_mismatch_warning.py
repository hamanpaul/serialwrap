"""#154：client↔daemon 版本不一致時，CLI 在 stderr 印一行警告（勿擋）。

沿用 `tests/test_attach_error_surface.py` 的 `_StubClient` + redirect_stdout/stderr
手法，跳過真實 daemon／socket。
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from sw_core.cli import main as cli_main

_REPO_VERSION = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
_MISMATCH_MARKER = "版本"  # `_warn_version_mismatch` 訊息固定含這個詞


class _StubClient:
    """替換 sw_core.cli.rpc_call，回固定回應（沿用 test_attach_error_surface._StubClient）。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def __call__(self, endpoint: str, method: str, params: dict | None = None,
                 timeout_s: float = 5.0, retries: int = 0) -> object:
        return self.responses.get(method, {"ok": True})


def _run(argv: list[str], stub: _StubClient) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sw_core.cli.rpc_call", stub):
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestRunRpcVersionMismatchWarning(unittest.TestCase):
    """`_run_rpc()` 路徑（多數子命令，如 `session attach`）。"""

    def test_version_mismatch_warns_on_success(self) -> None:
        stub = _StubClient({"session.attach": {"ok": True, "version": "9.9.9"}})
        rc, out, err = _run(["session", "attach", "--selector", "COM0"], stub)
        self.assertEqual(rc, 0, "成功語意不受影響（勿擋）")
        self.assertIn(_REPO_VERSION, err)
        self.assertIn("9.9.9", err)
        # stdout 仍是乾淨可解析 JSON
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])

    def test_version_match_emits_no_warning(self) -> None:
        stub = _StubClient({"session.attach": {"ok": True, "version": _REPO_VERSION}})
        rc, _out, err = _run(["session", "attach", "--selector", "COM0"], stub)
        self.assertEqual(rc, 0)
        self.assertNotIn(_MISMATCH_MARKER, err)

    def test_missing_version_field_is_silent_backward_compat(self) -> None:
        """舊版 daemon（本欄位加入前部署）無此欄位 → 不拋例外、不印警告。"""
        stub = _StubClient({"session.attach": {"ok": True}})
        rc, _out, err = _run(["session", "attach", "--selector", "COM0"], stub)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_failure_response_still_warns_and_keeps_94_error_line(self) -> None:
        """ok:False 且版本不同：#94 的 failed 行與新版本不一致行須共存、互不覆蓋。"""
        stub = _StubClient({
            "session.attach": {"ok": False, "error_code": "SOME_ERROR", "version": "9.9.9"},
        })
        rc, _out, err = _run(["session", "attach", "--selector", "COM0"], stub)
        self.assertEqual(rc, 2, "exit code 仍只由 ok 決定，不受版本比對影響")
        self.assertIn("failed: SOME_ERROR", err)
        self.assertIn("9.9.9", err)
        self.assertIn(_REPO_VERSION, err)


class TestDispatchEventVersionMismatchWarning(unittest.TestCase):
    """`_dispatch_event()` 路徑（`serialwrap event ...`）——證明覆蓋範圍不止 `_run_rpc`。"""

    def test_event_list_warns_on_mismatch(self) -> None:
        stub = _StubClient({"event.rule_list": {"ok": True, "rules": [], "version": "9.9.9"}})
        rc, out, err = _run(["event", "list"], stub)
        self.assertEqual(rc, 0)
        self.assertIn("9.9.9", err)
        self.assertIn(_REPO_VERSION, err)
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])

    def test_event_list_no_warning_on_match(self) -> None:
        stub = _StubClient({"event.rule_list": {"ok": True, "rules": [], "version": _REPO_VERSION}})
        rc, _out, err = _run(["event", "list"], stub)
        self.assertEqual(rc, 0)
        self.assertNotIn(_MISMATCH_MARKER, err)


if __name__ == "__main__":
    unittest.main()
