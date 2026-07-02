# tests/conftest.py
"""#120 三層防線：live 資源快照 → 強制 env 隔離 → per-test STATE_PATH patch → session-finish gate。

時序關鍵：本檔 module top-level 於 pytest collection 之前執行、早於任何測試模組 import——
sw_core/constants.py 的路徑常數是 import-time 凍結，fixture 階段才 setenv 一律太遲。
限制（載明於 CLAUDE.md）：python3 -m unittest discover 不載入本檔，該跑法的 state 維度
由 8 檔 per-file 隔離（tests/state_iso.py）補上。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

import liveguard

# ---- 第 0 步：live 快照（必須在覆寫任何 env 之前）----
_MODE = os.environ.get("SERIALWRAP_LIVE_GATE", "strict").strip().lower()
_SHELL_WAL_DIR = os.environ.get("SERIALWRAP_WAL_DIR")  # 覆寫前的外層 shell 值
_PRE_STATE = liveguard.snap_file(liveguard.live_state_path())
_PRE_WAL = liveguard.snap_file(liveguard.live_wal_path())
_PRE_CONFIG = liveguard.snap_file(liveguard.live_config_path())
_PRE_SHELL_WAL = (
    liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")) if _SHELL_WAL_DIR else None
)
_PRE_DAEMON = liveguard.snap_daemon()

# ---- 第 1 層：強制 env 隔離（硬覆寫——外層 shell 可能 export 指向 live 的值）----
_ISO_ROOT = tempfile.mkdtemp(prefix="sw-pytest-iso-")


def _iso(name: str) -> str:
    path = os.path.join(_ISO_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


os.environ["SERIALWRAP_STATE_DIR"] = _iso("state")
os.environ["SERIALWRAP_RUN_DIR"] = _iso("run")
os.environ["SERIALWRAP_WAL_DIR"] = _iso("wal")
os.environ["SERIALWRAP_CONFIG_DIR"] = _iso("config")
os.environ["SERIALWRAP_LOG_DIR"] = _iso("blog")
os.environ["SERIALWRAP_EVENTS_DIR"] = _iso("events.d")
os.environ["SERIALWRAP_EVENTS_RUNTIME_DIR"] = _iso("events-rt")
# 空目錄：防 in-process 動態偵測抓到真板（two-reader）
os.environ["SERIALWRAP_BY_ID_DIR"] = _iso("by-id")
os.environ["SERIALWRAP_BY_PATH_DIR"] = _iso("by-path")

# 這些變數優先序高於上方已覆寫目錄的推導值（如 SERIALWRAP_ENDPOINT 蓋過 RUN_DIR 推導的
# socket、PROFILE_DIR 蓋過 CONFIG_DIR/profiles）——外層 shell 若 export 會穿透隔離。
# pop 使其回歸「由已隔離目錄推導」的 default；hard-override 反而要在此重算一次路徑。
for _k in (
    "SERIALWRAP_ENDPOINT",
    "SERIALWRAP_PROFILE_DIR",
    "SERIALWRAP_TTYMCU_PATH",
    "SERIALWRAP_EVENTS_LOG_PATH",
    "SERIALWRAP_DATA_DIR",
):
    os.environ.pop(_k, None)

# tripwire：第 1 層的前提是 sw_core.constants（import-time 凍結路徑）尚未被載入；
# 若 conftest 頂層 import 鏈提前拉進它，隔離會全然無聲地失效——在此顯式釘住。
assert "sw_core.constants" not in sys.modules, (
    "sw_core.constants 已於 env 隔離前被 import——第 1 層隔離失效（檢查 conftest 頂層 import 鏈）"
)


# ---- 第 2 層：per-test STATE_PATH patch（消除順序耦合）----
@pytest.fixture(autouse=True)
def _isolate_state_path(tmp_path, monkeypatch):
    import sw_core.session_manager as sm

    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "state.json"))
    yield


# ---- 第 3 層：session-finish live guard ----
def pytest_sessionfinish(session, exitstatus):
    results = [
        ("state", liveguard.classify_state(
            _PRE_STATE, liveguard.snap_file(liveguard.live_state_path()), mode=_MODE)),
        ("wal", liveguard.classify_wal(
            _PRE_WAL, liveguard.snap_file(liveguard.live_wal_path()))),
        ("config", liveguard.classify_config(
            _PRE_CONFIG, liveguard.snap_file(liveguard.live_config_path()))),
        ("daemon", liveguard.classify_daemon(_PRE_DAEMON, liveguard.snap_daemon())),
    ]
    if _PRE_SHELL_WAL is not None:
        results.append(("shell-wal", liveguard.classify_shell_wal(
            _PRE_SHELL_WAL, liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")))))

    tr = session.config.pluginmanager.get_plugin("terminalreporter")

    def _emit(line: str, *, red: bool = False, yellow: bool = False) -> None:
        if tr is not None:
            tr.write_line(line, red=red, yellow=yellow)
        else:
            print(line, file=sys.stderr)  # reporter 缺席時 gate FAIL 不得無聲

    failed = False
    for name, (verdict, reason) in results:
        line = f"[live-guard:{name}] {verdict}: {reason}"
        if verdict == liveguard.VERDICT_FAIL:
            failed = True
        if verdict != liveguard.VERDICT_PASS:
            _emit(line, red=(verdict == liveguard.VERDICT_FAIL),
                  yellow=(verdict == liveguard.VERDICT_WARN))
    if failed:
        _emit("[live-guard] 偵測到 live 資源被測試觸碰（#120 回歸）——詳見上方各維度", red=True)
        # 只在原 exitstatus 乾淨時升為 1——不蓋掉 interrupted(2)/internal error(3) 等更高語意
        if session.exitstatus == 0:
            session.exitstatus = 1
    shutil.rmtree(_ISO_ROOT, ignore_errors=True)
