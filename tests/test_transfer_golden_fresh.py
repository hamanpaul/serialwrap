"""P8-T3：file_transfer golden 新鮮度——重跑捕捉腳本的輸出須與 contracts/golden 逐位元組相同。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]


@pytest.mark.skipif(
    sys.version_info[:3] != (3, 12, 3),
    reason="golden 以 CPython 3.12.3 為基準（CI 釘 3.12.3；其他版本為證據不足，非 PASS）",
)
def test_file_transfer_golden_is_fresh(tmp_path):
    target = tmp_path / "file_transfer.ndjson"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/rust/capture_transfer_golden.py"), "--out", str(target)],
        check=True,
    )
    assert target.read_bytes() == (ROOT / "contracts/golden/file_transfer.ndjson").read_bytes()
    assert len(target.read_text(encoding="utf-8").splitlines()) >= 600
