"""P9 Task 1 的 POSIX 專屬基礎測試。

由 `tests/test_stage3_base.py` 拆出：P0 規則（`tests/test_resource_markers.py`）禁止 `core_portable`
檔內的項目以平台原因 SKIP，故依平台分檔（比照 `test_stage3_canary_harvest_posix.py`）。
不需 POSIX 條件的 `proc.spawn` 測試已移至 `tests/test_stage3_base_proc.py`（Windows 亦執行）。
"""

from __future__ import annotations

import os
import sys

import pytest

from tools.stage3 import jsonio

# 模組層只放平台標記；資源標記依函式放置（每項恰一個資源標記，見 test_resource_markers）
pytestmark = [pytest.mark.posix_only]


@pytest.mark.memory_only
@pytest.mark.skipif(os.name == "nt", reason="POSIX 權限位元")
def test_atomic_write_keeps_existing_mode(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("old", encoding="utf-8")
    p.chmod(0o644)
    jsonio.atomic_write_text(p, "new")
    assert p.stat().st_mode & 0o777 == 0o644


@pytest.mark.process_rpc
@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="需可建立唯讀目錄的非 root POSIX")
def test_spawn_fails_fast_when_log_unwritable(tmp_path):
    from tools.stage3 import proc

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        with pytest.raises(OSError):
            proc.spawn([sys.executable, "-c", "import time;time.sleep(30)"], cwd=ro)
    finally:
        ro.chmod(0o755)


# ---- 審查補強 R-T1：新檔權限須依 umask；spawn 的寫檔失敗不得讓子程序卡在滿的管線 ----

@pytest.mark.memory_only
@pytest.mark.skipif(os.name == "nt", reason="POSIX 權限位元")
def test_atomic_write_new_file_respects_umask(tmp_path):
    old = os.umask(0o027)
    try:
        jsonio.atomic_write_text(tmp_path / "new.json", "x")
    finally:
        os.umask(old)
    assert (tmp_path / "new.json").stat().st_mode & 0o777 == 0o640   # 0o666 & ~0o027


@pytest.mark.process_rpc
def test_run_cmd_timeout_keeps_partial_stdout():
    # 逾時後 POSIX 的 subprocess.run 會把已讀到的輸出附在 TimeoutExpired 上；Windows 語意未在此固定，故留在 POSIX 檔
    from tools.stage3.proc import run_cmd

    code = "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(30)"
    res = run_cmd([sys.executable, "-c", code], timeout=3.0)
    assert res.rc == -1 and res.timed_out
    assert "partial" in res.stdout
