---
type: fix
scope: ci
---
修正 hosted CI 的 Python 相依、跨平台測試 fixture 與 Windows 路徑處理；將依賴 Bash／POSIX 的測項標為 `posix_only`，並保留 Rust 專屬測項在 Rust 輪執行。
