# tests/conftest.py
"""#120 三層防線：live 資源快照 → 強制 env 隔離 → per-test STATE_PATH patch → session-finish gate。

時序關鍵：本檔 module top-level 於 pytest collection 之前執行、早於任何測試模組 import——
sw_core/constants.py 的路徑常數是 import-time 凍結，fixture 階段才 setenv 一律太遲。
限制（載明於 CLAUDE.md）：python3 -m unittest discover 不載入本檔，該跑法的 state 維度
由 13 檔 per-file 隔離（tests/state_iso.py）補上。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

import ab_env  # noqa: F401  黑箱 A/B 選擇器須在 pop 前擷取
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
os.environ.pop("SERIALWRAP_TEST_SYSTEM_CONFIG_PATH", None)
os.environ["SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"] = os.path.join(_iso("system-config"), "config.yaml")
os.environ["SERIALWRAP_LOG_DIR"] = _iso("blog")
os.environ["SERIALWRAP_EVENTS_DIR"] = _iso("events.d")
os.environ["SERIALWRAP_EVENTS_RUNTIME_DIR"] = _iso("events-rt")
# 空目錄：防 in-process 動態偵測抓到真板（two-reader）
os.environ["SERIALWRAP_BY_ID_DIR"] = _iso("by-id")
os.environ["SERIALWRAP_BY_PATH_DIR"] = _iso("by-path")
os.environ["SERIALWRAP_INSTALL_ROOT"] = _iso("install")

# 這些變數優先序高於上方已覆寫目錄的推導值（如 SERIALWRAP_ENDPOINT 蓋過 RUN_DIR 推導的
# socket、PROFILE_DIR 蓋過 CONFIG_DIR/profiles）——外層 shell 若 export 會穿透隔離。
# pop 使其回歸「由已隔離目錄推導」的 default；hard-override 反而要在此重算一次路徑。
for _k in (
    "SERIALWRAP_ENDPOINT",
    "SERIALWRAP_PROFILE_DIR",
    "SERIALWRAP_TTYMCU_PATH",
    "SERIALWRAP_EVENTS_LOG_PATH",
    "SERIALWRAP_DATA_DIR",
    "SERIALWRAP_CLI_EXE",
    "SERIALWRAP_DAEMON_EXE",
    "SERIALWRAP_AB_LABEL",
    "SERIALWRAP_DELEGATE_DEPTH",
    "SERIALWRAP_DELEGATE_ARGV_SHA",
    "SW_STUB_LOG",
    "SW_STUB_SCRIPT",
    "SW_DELEGATE_MARKER",
    "SERIALWRAP_CONFIG",
    "SERIALWRAP_TEST_DEVICE_ALLOWLIST",
    "SERIALWRAP_RELEASE_MANIFEST",
):
    os.environ.pop(_k, None)

# tripwire：第 1 層的前提是 sw_core.constants（import-time 凍結路徑）尚未被載入；
# 若 conftest 頂層 import 鏈提前拉進它，隔離會全然無聲地失效——在此顯式釘住。
# 用 raise 而非 assert：python3 -O 下 assert 會靜默消失，tripwire 不得可被優化掉。
if "sw_core.constants" in sys.modules:
    raise RuntimeError(
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
            _PRE_WAL, liveguard.snap_file(liveguard.live_wal_path()), mode=_MODE)),
        ("config", liveguard.classify_config(
            _PRE_CONFIG, liveguard.snap_file(liveguard.live_config_path()))),
        ("daemon", liveguard.classify_daemon(_PRE_DAEMON, liveguard.snap_daemon(), mode=_MODE)),
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


# ---- marker 註冊（平台／資源分類；逐檔標註與稽核見 tests/test_resource_markers.py）----
_MARKERS = {
    "core_portable": "Linux 與 Windows 都必跑；禁止以平台原因 SKIP；不得宣稱測到原生 UART",
    "posix_only": "只在 POSIX 執行（PTY、POSIX 鎖、/proc、/dev/ttyMCU、AF_UNIX）",
    "windows_native": "只在原生 Windows 執行（檔案分享旗標、鎖、行程、排程器、原生裝置介面）",
    "memory_only": "只用行程內物件與暫存檔；不開 socket、子行程、PTY 或裝置",
    "process_rpc": "會啟動子行程或開真實 socket／RPC server",
    "posix_pty": "會開 POSIX PTY pair",
    "windows_com": "以原生 Windows serial backend 真正開啟 COM 裝置（socket 或記憶體 stream 不算）",
    "physical_uart": "需要實體 USB-UART 與 DUT",
    "supervisor_runtime": "需要真正運作中的 systemd／Task Scheduler／Windows service",
    "interactive_desktop": "需要登入中的桌面或終端多工器（tmux 等）",
}


def pytest_configure(config):
    for name, desc in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")
    # P4-T1：P4 驗收案例綁定標記（對應 tests/acceptance/p4_case_map.yaml）
    config.addinivalue_line("markers", "case(id): P4 驗收案例綁定（對應 p4_case_map.yaml）")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """依平台與擁有者提供的 COM pair 條件標記測試 skip。"""
    for item in items:
        if "windows_native" in item.keywords and os.name != "nt":
            item.add_marker(
                pytest.mark.skip(
                    reason="windows_native：請以 scripts/stage0/win-pytest.sh 執行（證據不足，非 PASS）"
                )
            )
        if "posix_only" in item.keywords and os.name == "nt":
            item.add_marker(pytest.mark.skip(reason="posix_only：Windows 不適用"))
        if "windows_com" in item.keywords and not os.environ.get("SW_TEST_COM_PAIR"):
            item.add_marker(
                pytest.mark.skip(
                    reason="BLOCKED-OWNER：需 OD-08 COM pair（SW_TEST_COM_PAIR；證據不足，非 PASS）"
                )
            )
