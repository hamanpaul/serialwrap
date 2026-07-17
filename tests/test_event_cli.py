from __future__ import annotations
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sw_core.cli import build_parser, main as cli_main


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def __call__(self, endpoint: str, method: str, params: dict | None = None, timeout_s: float = 5.0, retries: int = 0) -> object:
        # retries 為 #123 新增之 rpc_call 介面（僅唯讀白名單方法會用到），stub 記錄後忽略
        self.calls.append((method, dict(params or {})))
        return self.responses.get(method, {"ok": True, "method": method, "params": params})


class TestEventCli(unittest.TestCase):
    def test_event_status_calls_correct_method(self) -> None:
        stub = _StubClient()
        stub.responses["event.com_status"] = {"ok": True, "selector": "COM0", "enabled": True, "active_rules": []}
        with patch("sw_core.cli.rpc_call", stub):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli_main(["event", "status", "--selector", "COM0"])
            out = json.loads(buf.getvalue())
        self.assertEqual(stub.calls[0][0], "event.com_status")
        self.assertEqual(out["selector"], "COM0")

    def test_event_add_reads_file(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "rule.json")
            rule = {
                "schema_version": 1, "owner": "o", "name": "n", "kind": "tool",
                "selectors": ["COM0"], "pattern": {"kind": "contains", "value": "x"},
                "handler": {"exec": ["/bin/true"]},
            }
            with open(path, "w") as f:
                json.dump(rule, f)
            stub = _StubClient()
            stub.responses["event.rule_set"] = {"ok": True, **rule}
            with patch("sw_core.cli.rpc_call", stub):
                with redirect_stdout(io.StringIO()):
                    cli_main(["event", "add", "--file", path])
            self.assertEqual(stub.calls[0][0], "event.rule_set")
            self.assertEqual(stub.calls[0][1]["owner"], "o")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSessionSelfTestCli(unittest.TestCase):
    def test_parser_accepts_strict_human_lock(self) -> None:
        args = build_parser().parse_args(
            ["session", "self-test", "--selector", "COM0", "--strict-human-lock"]
        )
        self.assertTrue(args.strict_human_lock)

    def test_parser_defaults_strict_human_lock_false(self) -> None:
        args = build_parser().parse_args(["session", "self-test", "--selector", "COM0"])
        self.assertFalse(args.strict_human_lock)

    def test_parser_help_describes_strict_human_lock(self) -> None:
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buf):
                build_parser().parse_args(["session", "self-test", "--help"])

        help_text = buf.getvalue()
        self.assertIn("--strict-human-lock", help_text)
        self.assertIn("嚴格", help_text)
        self.assertIn("預設", help_text)
        self.assertIn("interactive lease", help_text)
        self.assertNotIn("raw interactive", help_text)

    def test_session_self_test_writes_strict_human_lock_to_rpc_params(self) -> None:
        stub = _StubClient()
        stub.responses["session.self_test"] = {"ok": True, "selector": "COM0"}
        with patch("sw_core.cli.rpc_call", stub):
            with redirect_stdout(io.StringIO()):
                rc = cli_main(
                    [
                        "session",
                        "self-test",
                        "--selector",
                        "COM0",
                        "--probe-timeout",
                        "4.5",
                        "--strict-human-lock",
                    ]
                )

        self.assertEqual(rc, 0)
        self.assertEqual(
            stub.calls[0],
            (
                "session.self_test",
                {"selector": "COM0", "timeout_s": 4.5, "strict_human_lock": True},
            ),
        )

    def test_session_self_test_writes_default_strict_human_lock_false_to_rpc_params(self) -> None:
        stub = _StubClient()
        stub.responses["session.self_test"] = {"ok": True, "selector": "COM0"}
        with patch("sw_core.cli.rpc_call", stub):
            with redirect_stdout(io.StringIO()):
                rc = cli_main(["session", "self-test", "--selector", "COM0"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            stub.calls[0],
            (
                "session.self_test",
                {"selector": "COM0", "timeout_s": 2.0, "strict_human_lock": False},
            ),
        )


if __name__ == "__main__":
    unittest.main()
