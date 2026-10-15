"""契約目錄工具套件（Rust 重構 Stage 0／P0）。

本套件只使用標準函式庫與 PyYAML，不得 import `sw_core`（§3.1、§11.3 黑箱獨立性）。
CLI 入口於後續 Task 提供（`python3 -m tools.contracts`）。
"""

from __future__ import annotations

CONTRACTS_DIR = "contracts"
