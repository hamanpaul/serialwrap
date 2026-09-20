from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _script_path(name: str) -> Path:
    path = _REPO_ROOT / "tools" / name
    if not path.is_file():
        pytest.fail(
            f"tools/{name} 尚未實作；Task 3 先以 RED 測試鎖定 bench 工具腳本的 CLI 契約"
        )
    return path


def _run_script(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update(env)
    return subprocess.run(
        ["bash", str(_script_path(name)), *args],
        cwd=_REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def test_bench_issue_help_and_missing_domain_contract() -> None:
    help_result = _run_script("bench-issue.sh", "--help")

    assert help_result.returncode == 0
    help_output = _combined_output(help_result).lower()
    assert "usage" in help_output
    assert "domain" in help_output

    run_result = _run_script("bench-issue.sh", "eit-02")

    assert run_result.returncode != 0
    assert "domain" in _combined_output(run_result).lower()


def test_bench_enroll_help_and_required_bundle_contract() -> None:
    help_result = _run_script("bench-enroll.sh", "--help")

    assert help_result.returncode == 0
    help_output = _combined_output(help_result).lower()
    assert "usage" in help_output
    assert "--bundle" in help_output

    run_result = _run_script("bench-enroll.sh")

    assert run_result.returncode != 0
    assert "bundle" in _combined_output(run_result).lower()
