from __future__ import annotations

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
