"""黑箱契約 harness 的 pytest 設定。

提供獨立執行（--confcutdir）與完整 suite 共存之設定：
- 註冊黑箱非平台 marker 與標準平台／資源 marker
- 動態載入 L1 報告 plugin（不經 tests 套件 import，避免污染 sys.modules）
- 提供 env、stubd、py_daemon、faketarget 等 fixtures
- 系統 config 穿透隔離防護（_no_system_config）
- 受測 artifact 身分交叉檢查（_artifact_identity）
- 獨立執行時保證無產品碼 import（pytest_sessionfinish 斷言）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# P4-T1 管理面夾具：獨立執行（--confcutdir）時不經根 conftest，故於本檔載入。
from mgmt_fixtures import (  # noqa: E402,F401
    live_daemon,
    mgmt_bundle,
    no_delegation,
    stub_log,
    stub_path,
)

_STANDARD_MARKERS = {
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

_BLACKBOX_MARKERS = {
    "rust_only": "僅 Rust 輪執行；is_rust() 為假時 skip（原因 rust-only contract）",
    "needs_py_daemon": "需要 Python daemon 基線",
    "needs_delegation": "Rust 輪需要 Task 16 委派（Task 14 以 -m 'not needs_delegation' 排除）",
    "case": "P4 驗收案例綁定（對應 tests/acceptance/p4_case_map.yaml）",
}


def pytest_configure(config: pytest.Config) -> None:
    for name, desc in _BLACKBOX_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")
    for name, desc in _STANDARD_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")

    # L1 報告 plugin：不經 tests 套件 import，直接從路徑載入
    if not config.pluginmanager.has_plugin("sw_resource_report"):
        rpt_env = os.environ.get("SW_RESOURCE_REPORT")
        if rpt_env:
            plugin_path = Path(__file__).resolve().parent.parent / "resource_report_plugin.py"
            if not plugin_path.exists():
                raise pytest.UsageError(f"SW_RESOURCE_REPORT 已指定但缺少 plugin: {plugin_path}")
            import importlib.util
            spec = importlib.util.spec_from_file_location("resource_report_plugin", plugin_path)
            if not (spec and spec.loader):
                raise pytest.UsageError(f"無法載入 plugin spec: {plugin_path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            config.pluginmanager.register(mod, "sw_resource_report")


def pytest_runtest_setup(item: pytest.Item) -> None:
    # 獨立執行（--confcutdir）不載入 root conftest 的平台 skip；Windows 上 posix_only 案例以
    # applicability.json 的鍵 "posix_only" 為原因 SKIP，比較器才會判為合法的不適用。
    if item.get_closest_marker("posix_only") and os.name == "nt":
        pytest.skip("posix_only")
    if item.get_closest_marker("rust_only"):
        from bb_harness import is_rust
        if not is_rust():
            pytest.skip("rust-only contract")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    from bb_harness import isolated_env
    return isolated_env(tmp_path)


@pytest.fixture
def stubd(tmp_path: Path):
    from bb_stubd import StubDaemon
    made: list[StubDaemon] = []

    def _make(script: dict, *, transport: str = "tcp" if os.name == "nt" else "unix") -> StubDaemon:
        wd = tmp_path / f"stub{len(made)}"
        wd.mkdir(parents=True, exist_ok=True)
        d = StubDaemon(wd, script, transport=transport)
        d.start()
        made.append(d)
        return d

    yield _make
    for d in made:
        d.stop()


@pytest.fixture
def py_daemon(tmp_path: Path, env: dict[str, str]):
    from bb_daemon import PyDaemon
    daemons: list[PyDaemon] = []

    def _make(
        *,
        profile_dir: Path | None = None,
        socket: str | None = None,
        lock: Path | None = None,
        custom_env: dict[str, str] | None = None,
    ) -> PyDaemon:
        pdir = profile_dir or (tmp_path / f"profiles{len(daemons)}")
        pdir.mkdir(parents=True, exist_ok=True)
        sock = socket or str(tmp_path / f"py_daemon{len(daemons)}.sock")
        lck = lock or (tmp_path / f"py_daemon{len(daemons)}.lock")
        d = PyDaemon(custom_env or env, profile_dir=pdir, socket=sock, lock=lck)
        daemons.append(d)
        return d

    yield _make
    for d in daemons:
        d.stop()


@pytest.fixture
def faketarget(tmp_path: Path):
    from bb_faketarget import FakeTarget
    ft = FakeTarget(tmp_path / "fake-workdir")
    yield ft
    ft.stop()


@pytest.fixture(scope="session", autouse=True)
def _no_system_config() -> None:
    if os.name == "nt":
        prog_data = os.environ.get("ProgramData", r"C:\ProgramData")
        cfg = Path(prog_data) / "serialwrap" / "config.yaml"
    else:
        cfg = Path("/etc/serialwrap/config.yaml")
    if cfg.exists():
        pytest.fail("基礎設施錯誤：系統 config 會穿透隔離（SC-09），證據不足")


@pytest.fixture(scope="session", autouse=True)
def _artifact_identity() -> None:
    from bb_harness import cli_prefix, is_rust
    cli_env = os.environ.get("SERIALWRAP_CLI_EXE")
    if cli_env == "/bin/serialwrap" and not Path("/bin/serialwrap").exists():
        sw_bundle = os.environ.get("SW_BUNDLE")
        if sw_bundle:
            os.environ["SERIALWRAP_CLI_EXE"] = str(Path(sw_bundle) / "bin" / "serialwrap")
    prefix = cli_prefix()
    target_path = Path(prefix[-1])
    if not target_path.exists():
        pytest.fail(f"受測 CLI 不存在（{target_path}），證據不足")
    try:
        with open(target_path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        pytest.fail(f"受測 CLI 不可讀（{target_path}）：{e}，證據不足")

    rust_mode = is_rust()
    if rust_mode:
        is_elf = magic.startswith(b"\x7fELF")
        is_pe = magic.startswith(b"MZ")
        if not (is_elf or is_pe):
            pytest.fail("A/B 標籤與受測 artifact 不符")
        manifest_path = Path(os.path.realpath(target_path)).parent.parent / "release-manifest.json"
        if manifest_path.exists():
            try:
                mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                pytest.fail(f"release-manifest.json 解析失敗（{manifest_path}）：{e}，證據不足")
            if mdata.get("components", {}).get("cli", {}).get("implementation") != "rust":
                pytest.fail("A/B 標籤與受測 artifact 不符")
    else:
        is_py = target_path.name.endswith(".py")
        is_shebang = magic.startswith(b"#!")
        if not (is_py or is_shebang):
            pytest.fail("A/B 標籤與受測 artifact 不符")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    confcutdir = session.config.getoption("confcutdir", None)
    if confcutdir:
        here = Path(__file__).resolve().parent
        if Path(str(confcutdir)).resolve() == here:
            assert "sw_core" not in sys.modules, (
                f"sw_core leaked into blackbox test process: {[m for m in sys.modules if m.startswith('sw_core')]}"
            )
