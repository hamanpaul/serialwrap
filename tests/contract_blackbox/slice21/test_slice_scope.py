"""Slice 2.1 範圍清單（黑箱）：延後 method 一律回 METHOD_NOT_FOUND 且無副作用。

僅在 `SERIALWRAP_AB_LABEL` 以 `rust-s21` 開頭（Rust Slice 2.1 輪）時執行；Python 基線 skip。
deferred method 清單取自 `tests/acceptance/slice21_scope.yaml`（Task 0 凍結）。
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.cap_posix_pty

SCOPE = Path(__file__).resolve().parents[3] / "tests" / "acceptance" / "slice21_scope.yaml"


@pytest.mark.skipif(
    not os.environ.get("SERIALWRAP_AB_LABEL", "").startswith("rust-s21"),
    reason="範圍清單僅於 Rust Slice 2.1 輪執行（SERIALWRAP_AB_LABEL=rust-s21*）",
)
def test_deferred_methods(shell_daemon):
    import yaml

    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    methods = [row["method"] for row in scope["deferred_methods"]]
    assert len(methods) == 15
    seq0 = shell_daemon.sw("wal", "current-seq")["seq"]
    for method in methods:
        r = shell_daemon.rpc(method, {})
        assert r["ok"] is False, method
        assert r["error_code"] == "METHOD_NOT_FOUND", method
        assert r["method"] == method, method
        assert "version" in r, method
    assert shell_daemon.sw("wal", "current-seq")["seq"] == seq0, "延後 method 不得改變 WAL current_seq"
