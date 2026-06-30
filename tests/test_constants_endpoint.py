from __future__ import annotations
import importlib
import sys
import os

import pytest

# 測試開始前記錄真實平台值，供 _restore_constants 在 teardown 時使用
_REAL_PLATFORM: str = sys.platform
_REAL_OS_NAME: str = os.name


@pytest.fixture(autouse=True)
def _restore_constants():
    """autouse：每個測試後強制以真實平台值 reload sw_core.constants，
    避免 monkeypatch reload 污染後續測試或其他模組。
    無論 monkeypatch 與本 fixture 的 teardown 順序為何，均能正確還原。
    """
    yield
    _cur_platform = sys.platform
    _cur_os_name = os.name
    try:
        sys.platform = _REAL_PLATFORM  # type: ignore[assignment]
        os.name = _REAL_OS_NAME  # type: ignore[assignment]
        import sw_core.constants as c
        importlib.reload(c)
    finally:
        sys.platform = _cur_platform  # type: ignore[assignment]
        os.name = _cur_os_name  # type: ignore[assignment]


def test_default_endpoint_posix(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("os.name", "posix")
    import sw_core.constants as c
    importlib.reload(c)
    assert c.DEFAULT_ENDPOINT == c.SOCKET_PATH  # POSIX：AF_UNIX 檔路徑


def test_default_endpoint_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("os.name", "nt")
    import sw_core.constants as c
    importlib.reload(c)
    assert c.DEFAULT_ENDPOINT == "tcp://127.0.0.1:48700"
