"""黑箱測試：CLI help、usage 與錯誤訊息位元組相容（P3-T10）。

逐案比對 Python 3.12.3 產生的 cli_help golden，確保：
- rc（exit code）相同
- stdout 位元組相同（含換行符號依平台轉換）
- stderr 位元組相同
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bb_harness import NL, is_rust, python_baseline_interpreter, repo_root, run_cli

GOLDEN = repo_root() / "contracts" / "cli_help" / ("windows" if NL == b"\r\n" else "linux")
META = json.loads((repo_root() / "contracts" / "cli_help" / "_meta.json").read_text(encoding="utf-8"))
CASES = sorted(p for p in GOLDEN.glob("*.json") if p.name != "_meta.json")
# Stage 1B Rust-only 擴充（SC-22／SC-23／SC-24／SC-36，absolute_only）使這兩個群組的 `--help` 與 Python 基線必然不同；
# Rust 輪改以 `capture_cli_help.py --rust-only`（Python 3.12.3 argparse＋同一組擴充）擷取的 golden 為 oracle。
RUST_ONLY_HELP = repo_root() / "contracts" / "cli_help" / "rust_only"
RUST_ONLY_HELP_GROUPS = ("setup", "supervision-mode")
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


def test_platform_oracle_exists_and_boundary_values_are_native():
    assert CASES, f"證據不足：{GOLDEN.relative_to(repo_root())} 沒有平台 golden"
    for case_id in ("boundary_socket_connect", "boundary_endpoint_connect"):
        case_path = GOLDEN / f"{case_id}.json"
        case = json.loads(case_path.read_text(encoding="utf-8"))
        assert case["native"] is True, f"{case_id} 的全域選項值不可當作委派群組"


@pytest.mark.parametrize("case", list(_params()))
def test_help_usage_error_bytes(env, case):
    c = json.loads(case.read_text(encoding="utf-8"))
    # 解析後會連線的案例：POSIX 下 SERIALWRAP_RUN_DIR=/golden/run 與 `--sock /x` 皆不存在，
    # 連線必為 ENOENT，輸出可確定性比對（A/B 兩輪同 SKIP 會被比較器判證據不足）；
    # Windows 預設 endpoint 為 TCP loopback，可能碰到實際 daemon，才維持 SKIP。
    if c.get("post_parse_effects") and os.name == "nt":
        pytest.skip("解析後連線行為由其他黑箱案例覆蓋")
    r = run_cli(c["argv"], env=_env(env, c["columns"]))
    nl = NL.decode()
    argv = c["argv"]
    if is_rust() and len(argv) == 2 and argv[0] in RUST_ONLY_HELP_GROUPS and argv[1] == "--help":
        golden = RUST_ONLY_HELP / f"{argv[0]}.{c['columns']}.txt"
        assert golden.is_file(), f"證據不足：缺少 Rust-only help golden {golden.relative_to(repo_root())}"
        expected = golden.read_text(encoding="utf-8")
        assert (r.rc, r.stdout.decode(), r.stderr.decode()) == (0, expected.replace("\n", nl), "")
        return
    if c.get("post_parse_effects") and c["stderr"] == "":
        # capture_cli_help.py 對 `--sock /x session list` 只錄 rc 與 stdout（plan Task 10），stderr 不比對。
        assert (r.rc, r.stdout.decode()) == (c["rc"], c["stdout"].replace("\n", nl))
        return
    assert (r.rc, r.stdout.decode(), r.stderr.decode()) == (
        c["rc"],
        c["stdout"].replace("\n", nl),
        c["stderr"].replace("\n", nl),
    )
