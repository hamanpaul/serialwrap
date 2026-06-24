"""tests/test_thread_survival.py — #79 長壽 thread 存活性。

驗證單一 I/O/handler 例外不再殺死長壽 thread：WAL 寫入失敗 best-effort（不拋、標記 loss）；
WAL 輪替失敗 best-effort（不拋、標記 rotation_failed、仍寫入資料）；device watcher 迴圈在
poll 例外下續行。
"""
import base64
import json
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


def test_wal_rotation_failure_best_effort_does_not_kill_reader(tmp_path, monkeypatch):
    """WAL 輪替失敗（rotation path OSError）須被 append 含住，不逃出殺 RX reader（#79 Codex 必修）。

    原 _rotate_if_needed 在 append 的 try 之外執行，rotation 的 replace/fsync OSError 會逃出 append
    → 殺死 reader。改為自身 best-effort 含住：不拋、標 rotation_failed、續用既有檔且資料仍寫入。
    """
    w = WalWriter(wal_dir=str(tmp_path), rotate_bytes=1)  # 極小上限 → 第二筆即觸發輪替
    w.append(com="COM0", direction="RX", source="device", payload=b"first")  # 養大檔案 > 1 byte

    orig_replace = os.replace

    def _boom_replace(src, dst, *a, **k):
        if str(src).endswith(("raw.wal.ndjson", "raw.mirror.log")):
            raise OSError("EIO during rotate")  # 模擬輪替 path 的 I/O 失敗
        return orig_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _boom_replace)

    rec = w.append(com="COM0", direction="RX", source="device", payload=b"second")  # 不得拋例外
    assert rec.get("rotation_failed") is True   # 輪替失敗被含住並標記（可觀測）
    assert rec["loss_flag"] is False            # 資料仍成功寫入（未因輪替失敗而丟）

    # 第二筆確實落盤（續用既有檔案）
    with open(w._wal_path, encoding="utf-8") as fp:
        recs = [json.loads(line) for line in fp if line.strip()]
    assert any(base64.b64decode(r["payload_b64"]) == b"second" for r in recs)


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
