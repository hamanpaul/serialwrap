"""P9 Task 1 的 `proc.spawn` 可攜測試（Windows 亦執行）。

`tools/stage3/suites/l3_realhw.py` 在 Windows 也使用 `proc.spawn`，因此不需 POSIX 條件的 spawn 測試
不得留在 `posix_only` 檔（審查 R-T1-4）。需 POSIX 權限位元的 `test_spawn_fails_fast_when_log_unwritable`
仍留在 `tests/test_stage3_base_posix.py`。
"""

from __future__ import annotations

import sys

import pytest

# 模組層只放平台 marker；資源 marker 依函式放置（每項恰一個資源標記，見 test_resource_markers）
pytestmark = [pytest.mark.core_portable]


@pytest.mark.process_rpc
def test_spawn_writes_output_to_pid_named_log(tmp_path):
    from tools.stage3 import proc

    handle = proc.spawn([sys.executable, "-c", "import sys;sys.stdout.write('hello')"], cwd=tmp_path)
    assert handle.wait(timeout=20) == 0
    assert (tmp_path / f"spawn-{handle.pid}.log").read_bytes() == b"hello"
    assert handle.log_error is None


@pytest.mark.process_rpc
def test_spawn_keeps_draining_after_log_write_failure(tmp_path):
    from tools.stage3 import proc

    # 子程序先等待，確保寫檔失敗發生在輸出之前；輸出 1 MiB 遠大於管線緩衝，若不排空會卡住
    code = "import sys,time;time.sleep(0.5);sys.stdout.buffer.write(b'x' * (1 << 20));sys.stdout.flush()"
    handle = proc.spawn([sys.executable, "-c", code], cwd=tmp_path)
    handle._log.close()          # 模擬寫檔失敗：之後每次寫入都會丟 ValueError
    assert handle.wait(timeout=20) == 0
    assert handle.log_error
