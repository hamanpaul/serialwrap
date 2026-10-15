"""契約目錄工具套件（Rust 重構 Stage 0／P0）。

本套件只使用標準函式庫與 PyYAML，不得 import `sw_core`（§3.1、§11.3 黑箱獨立性）。
CLI 入口：`python3 -m tools.contracts`（extract／check／summary／skill-section／effective）。
"""

from __future__ import annotations

CONTRACTS_DIR = "contracts"
