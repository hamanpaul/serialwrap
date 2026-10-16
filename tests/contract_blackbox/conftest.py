"""黑箱契約 harness 的 pytest 設定（P3-T2 紅燈階段）。

紅燈階段只提供測試實際使用的三個 fixture（env／stubd／faketarget）與非平台 marker 註冊。
系統 config 防護、受測身分交叉檢查、L1 報告 plugin 載入，留待 Step 4 實作。
"""
from __future__ import annotations

import pytest


def pytest_configure(config):
    for name, desc in {
        "rust_only": "僅 Rust 輪執行；is_rust() 為假時 skip（原因 rust-only contract）",
        "needs_py_daemon": "需要 Python daemon 基線",
        "needs_delegation": "Rust 輪需要 Task 16 委派（Task 14 以 -m 'not needs_delegation' 排除）",
    }.items():
        config.addinivalue_line("markers", f"{name}: {desc}")


@pytest.fixture
def env(tmp_path):
    from bb_harness import isolated_env

    return isolated_env(tmp_path)


@pytest.fixture
def stubd(tmp_path):
    from bb_stubd import StubDaemon

    made = []

    def _make(script, *, transport="unix"):
        wd = tmp_path / f"stub{len(made)}"
        wd.mkdir(parents=True, exist_ok=True)
        d = StubDaemon(wd, script, transport=transport)
        made.append(d)
        return d

    yield _make
    for d in made:
        d.stop()


@pytest.fixture
def faketarget(tmp_path):
    from bb_faketarget import FakeTarget

    ft = FakeTarget(tmp_path / "fake-workdir")
    yield ft
    ft.stop()
