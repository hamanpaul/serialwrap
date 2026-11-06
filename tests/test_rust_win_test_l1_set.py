"""驗證 Windows Cargo runner 可選擇正確的 Stage 1 L1 凍結集合。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rust" / "win-test.sh"


@pytest.mark.parametrize(
    ("set_id", "packages"),
    [
        ("s1a_cargo", ["--package", "sw-proto", "--package", "sw-pal", "--package", "sw-cli"]),
        ("s1b_cargo", ["--package", "sw-pal", "--package", "sw-cli"]),
    ],
)
def test_win_test_dry_run_selects_l1_set_and_packages(set_id: str, packages: list[str]) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--l1-set", set_id, "--dry-run", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"L1 set: {set_id}" in result.stdout
    assert "cargo test --no-run" in result.stdout
    for package in packages[1::2]:
        assert f"--package {package}" in result.stdout


def test_win_test_rejects_unknown_l1_set() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--l1-set", "s2_cargo", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "不支援的 L1 cargo 集合 s2_cargo" in result.stderr
