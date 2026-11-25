from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
DESELECT = [
    "tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native",
    "tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
    "tests/contract_blackbox/test_p7_invariants.py::test_release_during_login_waits_for_handle_close",
]


def _deselected(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "--deselect"]


def test_three_stage2_gates_and_preflight_match_ci_deselects():
    """N3／OD-115：pytest_full 與 preflight 精確排除 CI 不承擔的 Python 節點。"""
    workflows = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    ci_steps = workflows["jobs"]["pytest"]["steps"]
    ci_commands = [
        shlex.split(step["run"])
        for step in ci_steps
        if isinstance(step, dict) and "python3 -m pytest -q tests/" in step.get("run", "")
    ]
    assert len(ci_commands) == 2
    assert all(_deselected(argv) == DESELECT for argv in ci_commands)

    policy = yaml.safe_load((ROOT / ".project-policy.yml").read_text(encoding="utf-8"))
    tests_step = next(step for step in policy["preflight"]["steps"] if step.get("name") == "tests")
    assert tests_step["argv"] == [
        "python3", "-m", "pytest", "-q", "tests/",
        "--deselect", DESELECT[0], "--deselect", DESELECT[1],
        "--deselect", DESELECT[2],
    ]
    # 全套實測 6449.73 秒，另加 15% 餘裕並進位至 7500 秒。
    assert tests_step["timeout_seconds"] == 7500
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for nodeid in DESELECT:
        assert f"--deselect {nodeid}" in release, f"release workflow 未排除 {nodeid}"

    sources = {}
    for relative in ("scripts/rust/slice21_gate.sh", "scripts/rust/p7_gate.sh", "scripts/rust/slice24_exit.sh"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        sources[relative] = source
        for nodeid in DESELECT:
            assert f"--deselect {nodeid}" in source, f"{relative} 未排除 {nodeid}"

    s21_record = next(
        line.strip() for line in sources["scripts/rust/slice21_gate.sh"].splitlines()
        if line.strip().startswith("record pytest_full ") and "--command" in line
    )
    for nodeid in DESELECT:
        assert f"--deselect {nodeid}" in s21_record, "s21 記錄列命令與實際 argv 不一致"

    shared_source = (ROOT / "scripts/rust/gate_shared.sh").read_text(encoding="utf-8")
    assert ('for item in lock["sets"]["s21_gate:pytest_full"]["linux"]["selector"]["argv"]:'
            in shared_source)
    shared_pytest = next(
        line.strip() for line in shared_source.splitlines()
        if 'python3 -m pytest "${PYTEST_ARGV[@]}"' in line
    )
    assert '--junitxml="$PYTEST_DATA/junit.xml"' in shared_pytest
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    shared_argv = lock["sets"]["s21_gate:pytest_full"]["linux"]["selector"]["argv"]
    p7_argv = lock["sets"]["p7_gate:python3-m-pytest-q-tests"]["linux"]["selector"]["argv"]
    assert shared_argv == p7_argv
    assert _deselected(shared_argv) == DESELECT
