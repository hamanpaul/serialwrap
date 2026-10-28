"""Slice 2.1 L1.5 bridge 回歸（P5 Task 21）的 pytest 設定。

只註冊單元 marker `cap_posix_pty`（與 `tests/contract_blackbox/slice21/conftest.py` 同名同義），
使 `test_slice21_l15.py` 的 `-m cap_posix_pty` 選取不產生未註冊 marker 警告；
平台／資源 marker 由 `tests/conftest.py` 的 `_MARKERS` 統一註冊。
"""
from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "cap_posix_pty: 需要 POSIX PTY pair（Slice 2.1 以 -m cap_posix_pty 選取）",
    )
