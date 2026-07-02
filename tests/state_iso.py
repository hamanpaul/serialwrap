# tests/state_iso.py
"""#120 per-file state 隔離 helper（unittest＋pytest 兩用）。

與 tests/conftest.py 第 2 層刻意冗餘：conftest 防 pytest 下的未來漏網；
本 helper 讓 python3 -m unittest（不載入 conftest）與單檔直跑也安全。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile


@contextlib.contextmanager
def isolated_state():
    import sw_core.session_manager as sm
    import sw_core.wal as wal_mod

    td = tempfile.mkdtemp(prefix="sw-state-iso-")
    orig_state, orig_wal = sm.STATE_PATH, wal_mod.WAL_DIR
    sm.STATE_PATH = os.path.join(td, "state.json")
    wal_mod.WAL_DIR = os.path.join(td, "wal")
    try:
        yield td
    finally:
        sm.STATE_PATH = orig_state
        wal_mod.WAL_DIR = orig_wal
        shutil.rmtree(td, ignore_errors=True)


def isolate_testcase(tc) -> str:
    """unittest.TestCase 的 setUp 內呼叫：patch STATE_PATH/WAL_DIR，tearDown 階段自動還原。"""
    cm = isolated_state()
    td = cm.__enter__()
    tc.addCleanup(cm.__exit__, None, None, None)
    return td
