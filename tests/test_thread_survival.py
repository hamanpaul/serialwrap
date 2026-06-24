"""tests/test_thread_survival.py — #79 長壽 thread 存活性。

驗證單一 I/O/handler 例外不再殺死長壽 thread：WAL 寫入失敗 best-effort（不拋、標記 loss）；
device watcher 迴圈在 poll 例外下續行。
"""
import os
import time

from sw_core.device_watcher import DeviceWatcher
from sw_core.wal import WalWriter


def test_wal_append_best_effort_on_write_error(tmp_path):
    """WAL 寫入失敗（ENOSPC 類）不得拋例外殺死 RX reader；標記 loss_flag、仍回傳 record（#79 STA-1）。"""
    w = WalWriter(wal_dir=str(tmp_path))
    if os.path.exists(w._wal_path):
        os.remove(w._wal_path)
    os.mkdir(w._wal_path)  # wal 檔路徑變目錄 → open("a") 失敗（IsADirectoryError ⊂ OSError）
    rec = w.append(com="COM0", direction="RX", source="device", payload=b"x")  # 不得拋例外
    assert rec["loss_flag"] is True


def test_device_watcher_loop_survives_poll_exception(tmp_path):
    """poll_once 例外不得殺死 watcher thread；須持續輪詢（hotplug/reconcile 不停擺）（#79 STA-2）。"""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("poll boom")

    w = DeviceWatcher(str(tmp_path), on_change=lambda a, r: None, poll_interval_s=0.02)
    w.poll_once = boom
    w.start()
    try:
        time.sleep(0.25)
        assert w._thread.is_alive()      # thread 存活（未被 poll 例外殺死）
        assert len(calls) >= 2           # 持續輪詢（每輪 catch 後續行）
    finally:
        w._stop_event.set()
        if w._thread is not None:
            w._thread.join(timeout=1.0)
