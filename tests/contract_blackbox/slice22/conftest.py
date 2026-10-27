"""Slice 2.2 console 黑箱案例的 pytest 設定（07 Task 11）。

以 06 `slice21/harness.py` 的 `start_daemon` 與 P3 `bb_faketarget.FakeTarget` 為基礎；同一批案例可對 Python 基線
與 Rust artifact（`SERIALWRAP_AB_LABEL=rust*`、`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`）執行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SLICE21 = str(Path(__file__).resolve().parents[1] / "slice21")
if _SLICE21 not in sys.path:
    sys.path.insert(0, _SLICE21)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "cap_posix_pty: 需要 POSIX PTY pair")


@pytest.fixture
def console_daemon(tmp_path: Path):
    """COM0 已 READY 的受測 daemon；yield `(daemon, FakeTarget)`，teardown 一律 stop。"""
    from harness import start_daemon

    d = start_daemon(tmp_path / "console")
    try:
        d.wait_state("COM0", "READY")
        yield d, d.targets["COM0"]
    finally:
        try:
            d.stop()
        except Exception:
            pass
