from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
DESELECT = [
    "tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native",
    "tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
]


def _deselected(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "--deselect"]


def test_three_stage2_gates_and_preflight_match_ci_deselects():
    """N3：pytest_full 與 preflight 只排除 CI 同樣未提供證據的兩個節點。"""
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
    ]
    assert tests_step["timeout_seconds"] == 1800

    sources = {}
    for relative in ("scripts/rust/slice21_gate.sh", "scripts/rust/p7_gate.sh", "scripts/rust/slice24_exit.sh"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        sources[relative] = source
        for nodeid in DESELECT:
            assert f"--deselect {nodeid}" in source, f"{relative} 未排除 {nodeid}"

    s21_record = next(
        line for line in sources["scripts/rust/slice21_gate.sh"].splitlines()
        if line.startswith("record pytest_full ")
    )
    for nodeid in DESELECT:
        assert f"--deselect {nodeid}" in s21_record, "s21 記錄列命令與實際 argv 不一致"
