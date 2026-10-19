"""Stage 0 測試鉤子（T21）子行程測試：release 持久化 kill（情境 a）與 commit 前暫停點 kill（情境 b）。

與 tests/test_stage0_hw_cases.py 分檔：本檔會啟動子行程（process_rpc），依 P0 T14 規則不得與 memory_only 混用同一模組。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

REPO_ROOT = Path(__file__).resolve().parents[1]


_KILL_CHILD_SRC = r'''
import json
import os
import sys
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter

tmp = Path(sys.argv[1])
mode = sys.argv[2]
profile = SessionProfile(profile_name="p", com="COM0", act_no=1, alias="lab+1",
                         device_by_id="/dev/serial/by-id/X", platform="prpl", uart=UartProfile())
mgr = SessionManager([profile], WalWriter(wal_dir=str(tmp / "wal")), on_ready=lambda _s: None,
                     on_detached=lambda _s: None, state_path=str(tmp / "state.json"))
print("after_ctor_alive", flush=True)
if mode == "release":
    mgr.release_device("COM0", source="agent:kill-test", reason="kill-after-save")
    print("release_returned_UNEXPECTED", flush=True)
sys.exit(0)
'''


def _run_kill_child(tmp_path: Path, mode: str) -> "subprocess.CompletedProcess[str]":
    """以子行程建構 SessionManager（可選 release）：隔離 STATE／RUN／CONFIG 與鉤子環境變數，避免污染外層 shell。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SERIALWRAP_")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "tests")])
    env["SERIALWRAP_STATE_DIR"] = str(tmp_path / "state-dir")
    env["SERIALWRAP_RUN_DIR"] = str(tmp_path / "run-dir")
    env["SERIALWRAP_CONFIG_DIR"] = str(tmp_path / "config-dir")
    env["SERIALWRAP_TEST_KILL_AFTER_SAVE"] = "1"
    tmp_path.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-c", _KILL_CHILD_SRC, str(tmp_path), mode],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def test_kill_hook_does_not_fire_during_isolated_ctor(tmp_path):
    """隔離實例建構 SessionManager（__init__ 內會 _save_state）不得觸發鉤子：daemon 必須能活到收到 RPC。"""
    proc = _run_kill_child(tmp_path, "ctor")
    assert proc.returncode == 0, f"建構子不應被鉤子終止：rc={proc.returncode} stderr={proc.stderr[-400:]}"
    assert "after_ctor_alive" in proc.stdout


def test_kill_hook_fires_after_release_persist_on_isolated_instance(tmp_path):
    """隔離實例 release 持久化落盤後、回應前以 137 終止；重啟後 state.json 已含該 released 項（情境 (a)）。"""
    proc = _run_kill_child(tmp_path, "release")
    assert proc.returncode == 137, f"release 持久化後應以 137 終止：rc={proc.returncode} stderr={proc.stderr[-400:]}"
    assert "release_returned_UNEXPECTED" not in proc.stdout
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    released = state.get("released") or {}
    assert any(meta.get("by_id") == "/dev/serial/by-id/X" for meta in released.values()), state
    # 重啟（以同一 state.json 重新建構）後仍為 RELEASED，不得自動 re-attach 已交出的裝置。
    from stage0_fakes import make_manager, make_profile

    restarted = make_manager(tmp_path, [make_profile(by_id="/dev/serial/by-id/X")])
    states = {s["com"]: s["state"] for s in restarted.list_sessions()}
    assert states.get("COM0") == "RELEASED", states


_PAUSE_CHILD_SRC = r'''
import sys
import threading
import time
from pathlib import Path

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter

tmp = Path(sys.argv[1])
profile = SessionProfile(profile_name="p", com="COM0", act_no=1, alias="lab+1",
                         device_by_id="/dev/serial/by-id/X", platform="prpl", uart=UartProfile())
mgr = SessionManager([profile], WalWriter(wal_dir=str(tmp / "wal")), on_ready=lambda _s: None,
                     on_detached=lambda _s: None, state_path=str(tmp / "state.json"))
threading.Thread(target=lambda: mgr.release_device("COM0", source="agent:kill-test", reason="kill-before-save"),
                 daemon=True).start()
time.sleep(600)
'''


def test_kill_before_release_commit_leaves_last_valid_state(tmp_path):
    """情境 (b)：release 持久化 commit 前以 kill -9 終止（確定性暫停點 arrived 後才 kill）；state.json 不得宣稱 RELEASED。"""
    import signal
    import time

    env = {k: v for k, v in os.environ.items() if not k.startswith("SERIALWRAP_")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "tests")])
    env["SERIALWRAP_STATE_DIR"] = str(tmp_path / "state-dir")
    env["SERIALWRAP_RUN_DIR"] = str(tmp_path / "run-dir")
    env["SERIALWRAP_CONFIG_DIR"] = str(tmp_path / "config-dir")
    pause_dir = tmp_path / "pause"
    env["SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE"] = str(pause_dir)
    proc = subprocess.Popen([sys.executable, "-c", _PAUSE_CHILD_SRC, str(tmp_path)], cwd=str(REPO_ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60.0
        while not (pause_dir / "arrived").exists():
            assert proc.poll() is None, f"子行程提前結束：{proc.stderr.read()[-400:]}"
            assert time.monotonic() < deadline, "未於 60 秒內抵達 commit 前暫停點（arrived 未出現）"
            time.sleep(0.05)
        # 暫停點已確認：此時 state.json 尚未寫入 release 項目（commit 前）。
        pre = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert not (pre.get("released") or {}), pre
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == -signal.SIGKILL
    post = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not (post.get("released") or {}), f"commit 前被殺不得留下 RELEASED 紀錄：{post}"
