"""uart_stdio_bridge 工具自測的共用設定（P2-T1）。

- 將 `src/` 加入 `sys.path`，使 `uart_stdio_bridge` 可被 import（測試一律在函式內 import）。
- 產品邊界守衛：整個工具自測 session 結束時，若 `sys.modules` 出現 `sw_core`，session 判 FAIL。
  （bridge、DUT 模擬器、比較器不得 import 產品模組，見 OD-07、§15.4.2。）
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

TOOL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_ROOT.parents[1]
SRC = TOOL_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 工具測試專用 marker（P2-T3 起由本檔註冊；helpers.py 內的 pytest_configure 不是 plugin，不會被呼叫）
_TOOL_MARKERS = (
    "posix_pty: 需要 POSIX PTY（Linux）的測試；非 Linux 由測試檔以 cap:posix_pty 跳過",
    "windows_com: 需要真實 COM pair（環境變數 SW_TEST_COM_PAIR，擁有者 host）的測試",
    "physical_uart: 需要實體 UART 的測試",
    "docker: 需要 Docker 容器的測試",
)


def pytest_configure(config):
    for line in _TOOL_MARKERS:
        config.addinivalue_line("markers", line)


def pytest_runtest_setup(item):
    # windows_com：未提供真實 COM pair 時顯示為 skipped，且訊息明示「非 PASS」（§11.2、§15.8.3）
    if item.get_closest_marker("windows_com") and not os.environ.get("SW_TEST_COM_PAIR"):
        pytest.skip("cap:windows_com 未提供（證據不足，非 PASS）")


def pytest_sessionfinish(session, exitstatus):
    # 產品邊界：若純為 bridge 工具自測 session，不得載入產品模組
    cli_args = getattr(session.config, "args", [])
    for arg in cli_args:
        if not arg or arg.startswith("-"):
            continue
        path_part = arg.split("::")[0]
        try:
            resolved = pathlib.Path(path_part).resolve()
            if not resolved.is_relative_to(TOOL_ROOT):
                return
        except Exception:
            pass
    leaked = sorted(m for m in sys.modules if m == "sw_core" or m.startswith("sw_core."))
    if leaked:
        print(f"[bridge-boundary] FAIL: 載入了產品模組 {leaked}")
        if exitstatus == 0:
            session.exitstatus = 1
