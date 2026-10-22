from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "func-test" / "runner.py"
    spec = importlib.util.spec_from_file_location("serialwrap_func_test_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_mode_selects_cases_and_scenarios_in_one_invocation(tmp_path, monkeypatch, capsys):
    runner = _load_runner()
    root = tmp_path / "func-test"
    (root / "cases").mkdir(parents=True)
    (root / "scenarios").mkdir()
    (root / "cases" / "plain.yaml").write_text(
        "meta:\n  name: plain case\n  category: selection\n", encoding="utf-8")
    (root / "scenarios" / "scenario.yaml").write_text(
        "meta:\n  name: selected scenario\n  scenario:\n"
        "    revision: '2026-10-23.1'\n    seed: 7\n"
        "    capabilities: [uart]\n    timeout_s: 1\n"
        "    invariants: []\n    expected: PASS\n", encoding="utf-8")
    monkeypatch.setattr(runner, "FUNC_TEST_DIR", root)

    rc = runner.main([
        "--list", "--case", "plain", "--scenario-case", "scenario",
    ])

    output = capsys.readouterr().out
    assert rc == 0
    assert "plain case" in output
    assert "selected scenario" in output
