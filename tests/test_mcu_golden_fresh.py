"""P8-T2 golden 新鮮度：重跑 capture_mcu_golden.py 的輸出必須與 contracts/golden/mcu_patterns.ndjson 逐位元組相同。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]
GOLDEN = ROOT / "contracts" / "golden" / "mcu_patterns.ndjson"


def test_mcu_golden_is_fresh():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rust" / "capture_mcu_golden.py"), "--stdout"],
        check=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.stdout == GOLDEN.read_bytes()
