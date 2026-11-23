"""Stage 3 測試工具套件（P9）。

本套件比照 `realhw/`：不 import `sw_core`，不連結任何 Rust crate，只經部署 artifact 的
CLI subprocess 與 JSON、WAL 檔與 OS 程序資訊觀測。Canary 收集器相關模組限 stdlib，
以便在 Windows 測試工作站以 zipapp 執行。
"""
