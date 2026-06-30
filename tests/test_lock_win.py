from __future__ import annotations

import os
import sys
import pytest

from sw_core import lock_win


def test_endpoint_alive_refuses(tmp_path, monkeypatch):
    """endpoint 可連時，acquire() 必須 raise DAEMON_ALREADY_RUNNING。"""
    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda ep: True)
    lk = lock_win.WindowsSingletonLock(str(tmp_path / "d.lock"), "tcp://127.0.0.1:48799")
    with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
        lk.acquire()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="msvcrt 檔鎖僅 Windows")
def test_stale_endpoint_acquires(tmp_path, monkeypatch):
    """endpoint 不可連（stale）時，acquire() 取得 msvcrt 檔鎖，release() 釋放。"""
    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda ep: False)
    lk = lock_win.WindowsSingletonLock(str(tmp_path / "d.lock"), "tcp://127.0.0.1:48798")
    lk.acquire()
    try:
        assert os.path.exists(str(tmp_path / "d.lock"))
    finally:
        lk.release()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="msvcrt 檔鎖互斥僅 Windows")
def test_two_locks_same_path_second_raises(tmp_path, monkeypatch):
    """同一 lock_path 的兩個 WindowsSingletonLock，第一個 acquire 成功後，
    第二個 acquire 應因 msvcrt 鎖互斥而 raise DAEMON_ALREADY_RUNNING。"""
    monkeypatch.setattr(lock_win, "_endpoint_alive", lambda ep: False)
    lock_path = str(tmp_path / "mutex.lock")
    lk1 = lock_win.WindowsSingletonLock(lock_path, "tcp://127.0.0.1:48797")
    lk2 = lock_win.WindowsSingletonLock(lock_path, "tcp://127.0.0.1:48797")
    lk1.acquire()
    try:
        with pytest.raises(RuntimeError, match="DAEMON_ALREADY_RUNNING"):
            lk2.acquire()
    finally:
        lk1.release()
