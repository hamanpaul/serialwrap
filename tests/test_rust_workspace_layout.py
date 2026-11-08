from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

CRATES = ["sw-proto", "sw-pal", "sw-core", "sw-daemon", "sw-cli", "sw-com0com-helper"]


@pytest.fixture(autouse=True)
def _cargo_available():
    # workspace 不存在時不 SKIP：紅燈必須是真的失敗。只有 cargo 缺席可 SKIP，
    # 且 Rust CI（Task 19）設 SW_REQUIRE_CARGO=1，此時 cargo 缺席也是 FAIL。
    if shutil.which("cargo") is None:
        if os.environ.get("SW_REQUIRE_CARGO") == "1":
            pytest.fail("SW_REQUIRE_CARGO=1 但找不到 cargo（證據不足，非 PASS）")
        pytest.skip("cargo 不存在（證據不足，非 PASS）")


def test_members_exist():
    for c in CRATES:
        assert (ROOT / "crates" / c / "Cargo.toml").is_file(), c


def test_dep_direction():
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/rust/check_dep_direction.py"),
            "--root",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_workspace_version_matches_VERSION():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    text = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    assert f'version = "{version}"' in text


def test_toolchain_pinned():
    t = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    assert 'channel = "1.96.1"' in t and "x86_64-pc-windows-gnu" in t


def test_cargo_config_has_no_runner_or_linker():
    cfg = ROOT / ".cargo" / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    assert "runner" not in text and "linker" not in text  # §0.7：由 win-test.sh／win-runner.sh 以 env 注入


# 凍結 s1b_cargo selector 不帶 --features。拿掉 sw-cli 自我依賴後，這兩支仍須被該 selector 編譯。
_DELEGATE_HOOK_TESTS = (
    "run_delegate_increments_depth_and_executes_once_with_three_environment_values",
    "delegate_mechanism_still_guards_recursion",
)
_DELEGATE_HOOK_NODEIDS = (
    "cargo:sw-cli::delegate_resolve::run_delegate_increments_depth_and_executes_once_with_three_environment_values",
    "cargo:sw-cli::mgmt_terminal::delegate_mechanism_still_guards_recursion",
)


def test_s1b_frozen_selector_collects_delegate_hook_tests():
    """官方 s1b selector 維持凍結 argv，且兩支委派鉤子測試出現在未開 feature 的收集清單。"""
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    linux = lock["sets"]["s1b_cargo"]["linux"]
    windows = lock["sets"]["s1b_cargo"]["windows"]
    assert linux["selector"]["argv"] == ["--workspace"]
    assert windows["selector"]["argv"] == ["--package", "sw-pal", "--package", "sw-cli"]
    for nodeid in _DELEGATE_HOOK_NODEIDS:
        assert nodeid in linux["nodeids"]
        assert nodeid in windows["nodeids"]

    listed = subprocess.run(
        [
            "cargo",
            "test",
            "-p",
            "sw-cli",
            "--offline",
            "--test",
            "delegate_resolve",
            "--test",
            "mgmt_terminal",
            "--",
            "--list",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    names = {
        line.split(":", 1)[0]
        for line in listed.stdout.splitlines()
        if line.endswith(": test") or ": test " in line
    }
    missing = [name for name in _DELEGATE_HOOK_TESTS if name not in names]
    assert not missing, f"未開 test-fakes 的收集缺少 {missing}\n{listed.stdout}"
