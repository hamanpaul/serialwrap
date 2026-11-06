"""排空窗口零 TX 斷言（`bb_l15.assert_no_tx_after`）的負向驗證。

L1.5 案例 `test_update_background_triggers_zero_tx_in_drain_window_l15` 只能在真實 rig 上觀察窗口，
無法在 CI 內製造「窗口後段才出現 TX」的情境。本檔以合成的 `wire.ndjson` 驗證比較器本身：
零增量必須通過；窗口後段出現 TX、或 console marker 出現於 TX，都必須失敗；RX 不計入 TX 增量。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

CONTRACT_DIR = Path(__file__).resolve().parent / "contract_blackbox"


def _bb_l15():
    if str(CONTRACT_DIR) not in sys.path:
        sys.path.insert(0, str(CONTRACT_DIR))
    import bb_l15

    return bb_l15


def _write_wire(evidence: Path, rows: list[tuple[str, bytes]]) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"dir": d, "payload_b64": base64.b64encode(p).decode("ascii")}) for d, p in rows]
    (evidence / "wire.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_assert_no_tx_after_passes_on_zero_increment(tmp_path: Path) -> None:
    bb_l15 = _bb_l15()
    evidence = tmp_path / "ev"
    _write_wire(evidence, [("TX", b"frozen-baseline"), ("RX", b"rx-before")])
    baseline = bb_l15.tx_bytes(evidence)
    _write_wire(evidence, [("TX", b"frozen-baseline"), ("RX", b"rx-before"), ("RX", b"rx-after")])
    bb_l15.assert_no_tx_after(evidence, baseline, b"DRAIN_WINDOW_MARKER")


def test_assert_no_tx_after_rejects_late_tx_in_window(tmp_path: Path) -> None:
    """窗口後段才出現的 TX（例如背景重探於第 2.6 秒寫出）必須失敗。"""
    bb_l15 = _bb_l15()
    evidence = tmp_path / "ev"
    _write_wire(evidence, [("TX", b"frozen-baseline")])
    baseline = bb_l15.tx_bytes(evidence)
    _write_wire(evidence, [("TX", b"frozen-baseline"), ("TX", b"late-probe")])
    with pytest.raises(AssertionError, match="凍結後 DUT TX 有增量"):
        bb_l15.assert_no_tx_after(evidence, baseline, b"DRAIN_WINDOW_MARKER")


def test_assert_no_tx_after_rejects_marker_in_tx(tmp_path: Path) -> None:
    """即使 TX 增量為零，若 console marker 已出現於基準 TX 亦必須失敗。"""
    bb_l15 = _bb_l15()
    evidence = tmp_path / "ev"
    _write_wire(evidence, [("TX", b"echo DRAIN_WINDOW_MARKER\n")])
    baseline = bb_l15.tx_bytes(evidence)
    with pytest.raises(AssertionError, match="marker 出現於 DUT TX"):
        bb_l15.assert_no_tx_after(evidence, baseline, b"DRAIN_WINDOW_MARKER")
