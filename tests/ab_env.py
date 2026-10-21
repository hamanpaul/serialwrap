"""黑箱 A/B 環境擷取。

模組層從 os.environ 擷取 A/B 選擇器變數；root conftest 於 pop 之前 import 本檔。
stdlib-only，不 import 任何 repo 模組。
"""
from __future__ import annotations

import os

AB_KEYS: tuple[str, ...] = (
    "SERIALWRAP_CLI_EXE",
    "SERIALWRAP_DAEMON_EXE",
    "SERIALWRAP_AB_LABEL",
    "SW_BASELINE_PYTHON",
)
AB_ENV: dict[str, str] = {k: os.environ[k] for k in AB_KEYS if k in os.environ}
