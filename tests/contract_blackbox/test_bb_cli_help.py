"""黑箱測試：CLI help、usage 與錯誤訊息位元組相容（P3-T10）。

逐案比對 Python 3.12.3 產生的 cli_help golden，確保：
- rc（exit code）相同
- stdout 位元組相同（含換行符號依平台轉換）
- stderr 位元組相同
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bb_harness import NL, is_rust, python_baseline_interpreter, repo_root, run_cli

GOLDEN = repo_root() / "contracts" / "cli_help" / ("windows" if NL == b"\r\n" else "linux")
META = json.loads((repo_root() / "contracts" / "cli_help" / "_meta.json").read_text(encoding="utf-8"))
CASES = sorted(p for p in GOLDEN.glob("*.json") if p.name != "_meta.json")
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


@pytest.fixture(scope="module", autouse=True)
def _dialect_guard():
    # Python 基線輪：受測直譯器必須是 golden 的方言；不符 → FAIL（證據不足），不得 SKIP
    if not is_rust():
        import subprocess

        v = subprocess.run(
            [*python_baseline_interpreter(), "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert v == META["python_version"], f"Python 基線 {v} ≠ golden 方言 {META['python_version']}（證據不足）"


def _params():
    for p in CASES:
        c = json.loads(p.read_text(encoding="utf-8"))
        marks = [] if c["native"] else [pytest.mark.needs_delegation]
        yield pytest.param(p, id=p.stem, marks=marks)


def _env(env, columns):
    e = dict(env)
    e.update(
        COLUMNS=str(columns),
        SERIALWRAP_CONFIG_DIR="/golden/config",
        SERIALWRAP_STATE_DIR="/golden/state",
        SERIALWRAP_RUN_DIR="/golden/run",
        HOME="/golden/home",
    )
    return e


@pytest.mark.parametrize("case", list(_params()))
def test_help_usage_error_bytes(env, case):
    c = json.loads(case.read_text(encoding="utf-8"))
    if c.get("post_parse_effects"):
        pytest.skip("解析後連線行為由其他黑箱案例覆蓋")
    r = run_cli(c["argv"], env=_env(env, c["columns"]))
    nl = NL.decode()
    assert (r.rc, r.stdout.decode(), r.stderr.decode()) == (
        c["rc"],
        c["stdout"].replace("\n", nl),
        c["stderr"].replace("\n", nl),
    )
