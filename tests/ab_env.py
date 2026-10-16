"""黑箱 A/B 環境擷取（P3-T2 紅燈階段 stub）。

實作（Step 4.1）：模組層 AB_ENV = {k: os.environ[k] for k in AB_KEYS if k in os.environ}，
不 import 任何 repo 模組；root conftest 於 pop 之前 import 本檔。
紅燈階段 AB_ENV 僅為占位空 dict，使 test_ab_env_bbproc.py 可 import 並以 monkeypatch 覆寫。
"""
from __future__ import annotations

AB_KEYS: tuple[str, ...] = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL")
AB_ENV: dict[str, str] = {}  # 紅燈 stub：實作時改為從 os.environ 擷取
