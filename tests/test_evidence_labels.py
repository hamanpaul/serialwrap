"""OD-15 證據層級標記（tests/acceptance/evidence_labels.py）的失敗測試（P2 Task 17，T17-1）。

純函式測試：不開子行程、不開 socket，只驗證封閉集合、fail-closed 規則與「docker／throwaway 證據不得滿足真板層級」。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _el():
    # 函式內 import：模組尚未實作時只讓本檔測試紅燈，不阻斷整套收集（§1 第 3 條）
    from tests.acceptance import evidence_labels  # noqa: PLC0415

    return evidence_labels


def test_layers_exact_closed_set():
    el = _el()
    assert el.LAYERS == frozenset({"L1.5-local", "L1.5-docker", "host-throwaway", "native-windows",
                                   "L2-bench", "L3-bench", "hw-bench"})
    assert el.REAL_LAYERS == frozenset({"L2-bench", "L3-bench", "hw-bench"})


def test_docker_layer_never_satisfies_real_board():
    el = _el()
    for real in ("L2-bench", "L3-bench", "hw-bench"):
        assert el.layer_satisfies("L1.5-docker", real) is False
        assert el.layer_satisfies("host-throwaway", real) is False
    assert el.layer_satisfies("L1.5-docker", "L1.5-local") is True
    assert el.layer_satisfies("L2-bench", "L2-bench") is True


def test_label_report_rejects_unknown_and_conflicting_layer():
    el = _el()
    # 未知層級必須拒絕（封閉集合，只增不改）
    with pytest.raises(ValueError):
        el.label_report({}, evidence_layer="L1.5", producer="run-l15.sh", topology="docker-2c", uart_kind="pty-fake")
    # 既有 evidence_layer 與參數不同 → 拒絕覆寫
    with pytest.raises(ValueError):
        el.label_report({"evidence_layer": "L2-bench"}, evidence_layer="L1.5-docker", producer="run-l15.sh",
                        topology="docker-2c", uart_kind="pty-fake")
    r = el.label_report({"x": 1}, evidence_layer="L1.5-docker", producer="run-l15.sh", topology="docker-2c",
                        uart_kind="pty-fake", image_digests={"serialwrap": "sha256:" + "a" * 64})
    assert r["evidence_layer"] == "L1.5-docker" and r["producer"] == "run-l15.sh"
    assert r["topology"] == "docker-2c" and r["uart_kind"] == "pty-fake"


def test_fail_closed_missing_layer_for_docker_and_throwaway_producers():
    el = _el()
    for producer in ("run-l15.sh", "run-systemd.sh", "run-longrun.sh", "run-supervision.sh", "win-task-throwaway.ps1"):
        with pytest.raises(ValueError, match="evidence_layer_missing"):
            el.validate_report_layer({"producer": producer})
    with pytest.raises(ValueError, match="evidence_layer_unknown"):
        el.validate_report_layer({"producer": "run-l15.sh", "evidence_layer": "L2"})
    assert el.validate_report_layer({"producer": "run-l15.sh", "evidence_layer": "L1.5-docker"}) == "L1.5-docker"


def test_assert_not_real_board():
    el = _el()
    for real in ("L2-bench", "L3-bench", "hw-bench"):
        for layer in ("L1.5-docker", "host-throwaway"):
            with pytest.raises(AssertionError):
                el.assert_not_real_board({"evidence_layer": layer}, real)
    with pytest.raises(AssertionError):
        el.assert_not_real_board({"evidence_layer": "L1.5-docker", "counts_as": ["L2-bench"]}, "L1.5-docker")
    el.assert_not_real_board({"evidence_layer": "L1.5-docker", "counts_as": ["L1.5-local"]}, "L1.5-docker")
