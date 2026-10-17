"""P4-T1：root conftest 的 A/B 標籤隔離契約（`SERIALWRAP_AB_LABEL` 於 pop 後仍被黑箱 harness 看見）。

子 pytest 以 `SERIALWRAP_AB_LABEL=rust-1b` 執行本檔的 child 測試；child 內 `bb_harness.is_rust()` 必須為真，
證明 `tests/ab_env.py` 在 root conftest pop 之前已擷取受測實作選擇器。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "tests" / "contract_blackbox"
CHILD_NODE = f"{Path(__file__).resolve()}::test_child_is_rust_round"


def test_child_is_rust_round() -> None:
    """只在子行程（`SW_P4_CHILD=1`）執行；父行程略過。"""
    if os.environ.get("SW_P4_CHILD") != "1":
        pytest.skip("僅供 test_ab_label_survives_root_conftest 啟動的子行程執行")
    sys.path.insert(0, str(CONTRACT_DIR))
    import bb_harness  # noqa: E402

    assert bb_harness.is_rust(), f"ab_label={bb_harness.ab_label()!r}"


def test_ab_label_survives_root_conftest() -> None:
    env = dict(os.environ, SERIALWRAP_AB_LABEL="rust-1b", SW_P4_CHILD="1")
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", CHILD_NODE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 passed" in res.stdout, res.stdout
