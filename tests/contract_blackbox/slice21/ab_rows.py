"""Slice 2.1 黑箱 A/B 的逐列標記（擁有者裁決 OD-37／OD-38；P5 第四輪）。

- `PY_NOT_APPLICABLE`：只在 Rust 判定的列；Python 輪標為 not_applicable（skip，不計 PASS、不刪案例）。
- `PY_DF_DIFFERENCE`：已核准 DF 的列；Rust 判定 DF 修正後行為，Python 輪標為 DF 差異（skip，不計 PASS）。
- `RUST_S21_DEFERRED`：DF 的 Rust 實作屬較晚切片／Phase 時，Slice 2.1 Rust 輪只登記不判定（skip，不計 PASS）。

skip 原因字串皆以分類前綴開頭（`not_applicable_python:`／`df_difference_python:`／`deferred_rust_s21:`），
供 gate／報告逐列計數；不得把這些列當成 PASS。
"""
from __future__ import annotations

import pytest

from bb_harness import ab_label, is_rust

PY_NOT_APPLICABLE: dict[str, str] = {
    "test_reject_paths_zero_tx[METHOD_NOT_FOUND]":
        "OD-38：Python 已實作 file.push（Slice 2.4），缺參數回 INVALID_ARGS；METHOD_NOT_FOUND 只對 Rust Slice 2.1 範圍成立。"
        "P8 實作 file.push 後本列改要求與 Python 一致的 INVALID_ARGS",
}
PY_DF_DIFFERENCE: dict[str, str] = {
    "test_recovery_flushes_queue":
        "DF-21（OD-37）：Python 0.4.0 現行 recover flush 有 q0 仍被送出的競態；Rust 原子 flush（全部 FLUSHED_BY_RECOVERY、零 TX）",
    "test_stale_context_not_replayed":
        "DF-20（OD-37）：Python 0.4.0 現行以舊 interactive_id 照送；Rust 回 INTERACTIVE_NOT_FOUND 且零 TX",
}
RUST_S21_DEFERRED: dict[str, str] = {
    "test_stale_context_not_replayed":
        "DF-20 的 Rust 實作屬 Slice 2.2 interactive lease（P6；behavior_changes.yaml applies_to.rust.stage=slice-2.2），"
        "Slice 2.1 只登記、留給 P6 判定",
}


def gate_row(row_id: str) -> None:
    """依目前 A/B label 套用逐列標記；未登記的列不受影響。"""
    if not is_rust():
        if row_id in PY_NOT_APPLICABLE:
            pytest.skip(f"not_applicable_python: {PY_NOT_APPLICABLE[row_id]}")
        if row_id in PY_DF_DIFFERENCE:
            pytest.skip(f"df_difference_python: {PY_DF_DIFFERENCE[row_id]}")
    elif ab_label().startswith("rust-s21") and row_id in RUST_S21_DEFERRED:
        pytest.skip(f"deferred_rust_s21: {RUST_S21_DEFERRED[row_id]}")
