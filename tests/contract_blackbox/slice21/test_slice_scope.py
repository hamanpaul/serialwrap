"""Slice 2.1 範圍清單（黑箱）：延後與未支援 method 一律回 METHOD_NOT_FOUND 且無副作用。

僅在 `SERIALWRAP_AB_LABEL` 以 `rust-s21` 開頭（Rust Slice 2.1 輪）時執行；Python 基線 skip。
deferred method 清單取自 `tests/acceptance/slice21_scope.yaml`（Task 0 凍結）；
P5 Task 0 雙審修正：同時涵蓋 `unsupported_catalog_methods`（`daemon.handoff_stop`，P8-T14 靠它讓更新與回滾 fail-closed）。
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.cap_posix_pty

SCOPE = Path(__file__).resolve().parents[3] / "tests" / "acceptance" / "slice21_scope.yaml"
# Rust daemon 目前交付的切片（`sw_core::SLICE`）。
DELIVERED_SLICE = "2.2"


def _slice_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(text).split("."))


def _ab_label() -> str:
    """A/B label：root conftest 會 pop `SERIALWRAP_AB_LABEL`，故以 pop 前擷取的 `tests/ab_env.AB_ENV` 為準
    （P5-T20 補強：原本只讀 os.environ，pytest 下恆為空而使 Rust 輪也 skip）；os.environ 仍作後備。"""
    try:
        from ab_env import AB_ENV
    except ImportError:
        AB_ENV = {}
    return AB_ENV.get("SERIALWRAP_AB_LABEL") or os.environ.get("SERIALWRAP_AB_LABEL", "")


@pytest.mark.skipif(
    not _ab_label().startswith("rust-s21"),
    reason="範圍清單僅於 Rust Slice 2.1 輪執行（SERIALWRAP_AB_LABEL=rust-s21*）",
)
def test_deferred_methods(shell_daemon):
    import yaml

    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    assert len(scope["deferred_methods"]) == 15
    # 增補 #24（07 Task 10）：Rust daemon 已交付 Slice 2.2（console 三個 method），只檢查晚於 2.2 的列。
    deferred = [row["method"] for row in scope["deferred_methods"] if _slice_key(row["slice"]) > _slice_key(DELIVERED_SLICE)]
    assert len(deferred) == 12
    unsupported = [row["method"] for row in scope["unsupported_catalog_methods"]]
    methods = deferred + unsupported
    seq0 = shell_daemon.sw("wal", "current-seq")["seq"]
    for method in methods:
        r = shell_daemon.rpc(method, {})
        assert r["ok"] is False, method
        assert r["error_code"] == "METHOD_NOT_FOUND", method
        assert r["method"] == method, method
        assert "version" in r, method
    assert shell_daemon.sw("wal", "current-seq")["seq"] == seq0, "延後 method 不得改變 WAL current_seq"
