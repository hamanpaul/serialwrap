from __future__ import annotations
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sw_core.cli import main as cli_main


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}

    def __call__(self, endpoint: str, method: str, params: dict | None = None, timeout_s: float = 5.0) -> object:
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


if __name__ == "__main__":
    unittest.main()
