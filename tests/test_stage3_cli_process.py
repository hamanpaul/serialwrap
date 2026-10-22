"""P9-T17 審查後補強：需要子程序的 CLI 檢查。

與 `test_stage3_cli.py` 分檔：該檔為 memory_only，子程序（process_rpc）測試不得與其同檔（`test_resource_markers`）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]


_STDLIB_ONLY_PROBE = r'''
import sys

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"yaml", "jsonschema", "realhw"}:
            raise ModuleNotFoundError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, _Block())
sys.path.insert(0, sys.argv[1])
from tools.stage3.__main__ import build_parser
build_parser().parse_args(["canary", "collect", "--cli", "c", "--ledger-dir", "l", "--policy", "p", "--platform", "linux", "--once"])
build_parser().parse_args(["od09", "check", "--acceptance", "a", "--run-started-at", "2026-10-21T00:00:00+00:00"])
print("ok")
'''


def test_build_parser_is_stdlib_only():
    # canary collect 以 /usr/bin/python3 常駐（systemd，canary.md §4），無 PyYAML／jsonschema 亦須可解析。
    proc = subprocess.run(
        [sys.executable, "-c", _STDLIB_ONLY_PROBE, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_stdout_is_utf8_under_non_utf8_locale(tmp_path):
    # 非 UTF-8 stdout（例如 Windows 重導向）下，含中文的 HarnessError JSON 仍須輸出（exit 2，不崩潰）。
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"schema_version": 2, "entries": [], "stage_targets": {}, "target_history": []}), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    environment = tmp_path / "env.json"
    environment.write_text("{}", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(
        [
            sys.executable, "-m", "tools.stage3", "evidence", "ingest",
            "--index", str(index), "--kind", "nope", "--item", "S3-ROLLBACK-LINUX", "--platform", "linux",
            "--report", str(report), "--evidence-root", str(tmp_path), "--environment", str(environment),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 2, proc.stderr.decode("utf-8", "replace")
    doc = json.loads(proc.stdout.decode("utf-8"))
    assert doc["ok"] is False
    assert doc["reason_code"] == "HARNESS_EVIDENCE_INGEST_INVALID"
