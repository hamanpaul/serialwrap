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
import stat
import sys
import tempfile

import pytest

import ab_env  # noqa: F401  黑箱 A/B 選擇器須在 pop 前擷取
import liveguard
import resource_report_plugin as _rrp  # 資源報告外掛（P2-T13）；本模組不 import sw_core

# ---- 第 0 步：live 快照（必須在覆寫任何 env 之前）----
_MODE = os.environ.get("SERIALWRAP_LIVE_GATE", "strict").strip().lower()
_SHELL_WAL_DIR = os.environ.get("SERIALWRAP_WAL_DIR")  # 覆寫前的外層 shell 值


def _snapshot_windows_user_home(profile: str) -> dict[str, object]:
    """快照 Windows 使用者家目錄中 setup 可能寫入的 serialwrap 靜態資產。"""
    if not profile or not os.path.isabs(profile) or not os.path.isdir(profile):
        raise RuntimeError("Windows USERPROFILE 不存在或不是絕對路徑，無法保護真實家目錄")

    def _metadata(path: str):
        info = os.lstat(path)
        return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _is_reparse_point(info) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & reparse_flag
        )

    def _snapshot_path(root: str):
        try:
            root_info = os.lstat(root)
        except FileNotFoundError:
            return None
        entries = [(".", _metadata(root))]
        if stat.S_ISDIR(root_info.st_mode) and not _is_reparse_point(root_info):
            def _raise_walk_error(error):
                raise error

            for current, directories, files in os.walk(
                root, topdown=True, followlinks=False, onerror=_raise_walk_error
            ):
                directories.sort()
                files.sort()
                for name in directories + files:
                    path = os.path.join(current, name)
                    info = os.lstat(path)
                    entries.append((os.path.relpath(path, root), _metadata(path)))
                    if name in directories and _is_reparse_point(info):
                        directories.remove(name)
        return tuple(entries)

    roots = {
        ".agents/skills/serialwrap": os.path.join(profile, ".agents", "skills", "serialwrap"),
        ".local/share/serialwrap": os.path.join(profile, ".local", "share", "serialwrap"),
    }
    snapshot = {label: _snapshot_path(path) for label, path in roots.items()}

    bin_dir = os.path.join(profile, ".local", "bin")
    try:
        serialwrap_names = sorted(name for name in os.listdir(bin_dir) if name.startswith("serialwrap"))
    except FileNotFoundError:
        serialwrap_names = []
    snapshot[".local/bin/serialwrap*"] = tuple(
        (name, _snapshot_path(os.path.join(bin_dir, name))) for name in serialwrap_names
    )
    return snapshot


_REAL_WINDOWS_USERPROFILE = os.environ.get("USERPROFILE") if os.name == "nt" else None
if os.name == "nt" and not _REAL_WINDOWS_USERPROFILE:
    raise RuntimeError("Windows USERPROFILE 未設定，無法執行隔離 pytest")
_LIVE_STATE_PATH = liveguard.live_state_path()
_LIVE_WAL_PATH = liveguard.live_wal_path()
_LIVE_CONFIG_PATH = liveguard.live_config_path()
_PRE_STATE = liveguard.snap_file(_LIVE_STATE_PATH)
_PRE_WAL = liveguard.snap_file(_LIVE_WAL_PATH)
_PRE_CONFIG = liveguard.snap_file(_LIVE_CONFIG_PATH)
_PRE_SHELL_WAL = (
    liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")) if _SHELL_WAL_DIR else None
)
_PRE_DAEMON = liveguard.snap_daemon()
_PRE_WINDOWS_USER_HOME = (
    _snapshot_windows_user_home(_REAL_WINDOWS_USERPROFILE)
    if _REAL_WINDOWS_USERPROFILE is not None
    else None
)


def _snap_live_daemon():
    """Windows 隔離後暫用原始 USERPROFILE，讓唯讀 daemon 探測沿用 live config 路徑。"""
    if _REAL_WINDOWS_USERPROFILE is None:
        return liveguard.snap_daemon()
    isolated_profile = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = _REAL_WINDOWS_USERPROFILE
    try:
        return liveguard.snap_daemon()
    finally:
        if isolated_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = isolated_profile


# deployed_content 量測用（R5-F4）：下方第 1 層會 pop 這兩個變數，須先保留
_rrp.capture_deployed_env(os.environ)

# ---- 第 1 層：強制 env 隔離（硬覆寫——外層 shell 可能 export 指向 live 的值）----
_ISO_ROOT = tempfile.mkdtemp(prefix="sw-pytest-iso-")


def _iso(name: str) -> str:
    path = os.path.join(_ISO_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


if os.name == "nt":
    _ISO_HOME = _iso("home")
    os.environ["USERPROFILE"] = _ISO_HOME
    os.environ["HOME"] = _ISO_HOME
    _HOME_DRIVE, _HOME_PATH = os.path.splitdrive(_ISO_HOME)
    if _HOME_DRIVE:
        os.environ["HOMEDRIVE"] = _HOME_DRIVE
        os.environ["HOMEPATH"] = _HOME_PATH or os.path.sep
    else:
        os.environ.pop("HOMEDRIVE", None)
        os.environ.pop("HOMEPATH", None)


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
    "SERIALWRAP_CLI_EXE",
    "SERIALWRAP_DAEMON_EXE",
    "SERIALWRAP_TEST_KILL_AFTER_SAVE",
    "SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE",
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
            _PRE_STATE, liveguard.snap_file(_LIVE_STATE_PATH), mode=_MODE)),
        ("wal", liveguard.classify_wal(
            _PRE_WAL, liveguard.snap_file(_LIVE_WAL_PATH), mode=_MODE)),
        ("config", liveguard.classify_config(
            _PRE_CONFIG, liveguard.snap_file(_LIVE_CONFIG_PATH))),
        ("daemon", liveguard.classify_daemon(_PRE_DAEMON, _snap_live_daemon(), mode=_MODE)),
    ]
    if _PRE_SHELL_WAL is not None:
        results.append(("shell-wal", liveguard.classify_shell_wal(
            _PRE_SHELL_WAL, liveguard.snap_file(os.path.join(_SHELL_WAL_DIR, "raw.wal.ndjson")))))
    if _PRE_WINDOWS_USER_HOME is not None:
        try:
            post_windows_home = _snapshot_windows_user_home(_REAL_WINDOWS_USERPROFILE)
        except (OSError, RuntimeError) as exc:
            results.append(("windows-userprofile", (
                liveguard.VERDICT_FAIL,
                f"Windows 家目錄快照無法完成（{type(exc).__name__}）",
            )))
        else:
            changed = sorted(
                label for label, before in _PRE_WINDOWS_USER_HOME.items()
                if post_windows_home.get(label) != before
            )
            if changed:
                results.append(("windows-userprofile", (
                    liveguard.VERDICT_FAIL,
                    "Windows 使用者家目錄受保護項目變更：" + ", ".join(changed),
                )))
            else:
                results.append(("windows-userprofile", (
                    liveguard.VERDICT_PASS,
                    "Windows 使用者家目錄受保護項目快照一致",
                )))

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
        if verdict != liveguard.VERDICT_PASS or name == "windows-userprofile":
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
    # 報告 hook 的唯一宿主；同名只註冊一次（完整 pytest tests/ 與黑箱 conftest 可能同時載入）
    if not config.pluginmanager.has_plugin(_rrp.PLUGIN_NAME):
        config.pluginmanager.register(_rrp, _rrp.PLUGIN_NAME)


def pytest_ignore_collect(collection_path, config):
    """Windows collection 前排除模組層 posix_only 的測試檔（避免頂層 import pty／termios 造成 collection error）。"""
    if _rrp.ignore_on_windows(collection_path, config):
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_make_collect_report(collector):
    """命令列明確指定的檔案不經 pytest_ignore_collect（pytest 對 initial path 不呼叫該 hook），此處同樣排除。

    被排除的模組不 import、不產生項目；路徑仍記入資源報告的 not_collected_platform（P2-T13）。
    """
    if isinstance(collector, pytest.Module) and _rrp.ignore_on_windows(collector.path, collector.config):
        return pytest.CollectReport(collector.nodeid, "passed", None, [])
    return None
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
            item.stash[_rrp.CAPABILITY_SKIP] = True
        if "posix_only" in item.keywords and os.name == "nt":
            item.add_marker(pytest.mark.skip(reason="posix_only：Windows 不適用"))
            item.stash[_rrp.CAPABILITY_SKIP] = True
        if "windows_com" in item.keywords and not os.environ.get("SW_TEST_COM_PAIR"):
            item.add_marker(
                pytest.mark.skip(
                    reason="BLOCKED-OWNER：需 OD-08 COM pair（SW_TEST_COM_PAIR；證據不足，非 PASS）"
                )
            )
            item.stash[_rrp.CAPABILITY_SKIP] = True
