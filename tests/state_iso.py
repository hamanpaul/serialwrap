# tests/state_iso.py
"""#120 per-file 隔離 helper（unittest＋pytest 兩用）：state／WAL／events 三類維度。

與 tests/conftest.py 第 2 層刻意冗餘：conftest 防 pytest 下的未來漏網；
本 helper 讓 python3 -m unittest（不載入 conftest）與單檔直跑也安全。
涵蓋維度（#121 F3 擴充）：session_manager.STATE_PATH、wal.WAL_DIR，以及
sw_core.service 模組層 EVENTS_DIR／EVENTS_RUNTIME_DIR／EVENTS_LOG_PATH
（`SerialwrapService` 建構時讀模組屬性注入 EngineDeps，patch 即生效）。
其餘雜項維度（LOG_DIR 等）仍為 pytest-only（conftest 第 1 層）。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile


@contextlib.contextmanager
def isolated_state():
    import sw_core.service as svc
    import sw_core.session_manager as sm
    import sw_core.wal as wal_mod

    td = tempfile.mkdtemp(prefix="sw-state-iso-")
    orig_state, orig_wal = sm.STATE_PATH, wal_mod.WAL_DIR
    orig_events = (svc.EVENTS_DIR, svc.EVENTS_RUNTIME_DIR, svc.EVENTS_LOG_PATH)
    sm.STATE_PATH = os.path.join(td, "state.json")
    wal_mod.WAL_DIR = os.path.join(td, "wal")
    svc.EVENTS_DIR = os.path.join(td, "events.d")
    svc.EVENTS_RUNTIME_DIR = os.path.join(td, "events-rt")
    svc.EVENTS_LOG_PATH = os.path.join(td, "events-rt", "events.ndjson")
    try:
        yield td
    finally:
        sm.STATE_PATH = orig_state
        wal_mod.WAL_DIR = orig_wal
        svc.EVENTS_DIR, svc.EVENTS_RUNTIME_DIR, svc.EVENTS_LOG_PATH = orig_events
        shutil.rmtree(td, ignore_errors=True)


def isolate_testcase(tc) -> str:
    """unittest.TestCase 的 setUp 內呼叫：patch STATE_PATH/WAL_DIR/EVENTS_*，tearDown 階段自動還原。"""
    cm = isolated_state()
    td = cm.__enter__()
    tc.addCleanup(cm.__exit__, None, None, None)
    return td
