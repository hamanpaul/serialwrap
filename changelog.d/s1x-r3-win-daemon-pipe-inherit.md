---
type: fix
scope: rust-refactor
---

Windows Rust CLI 以 on-demand／`daemon start` 啟動背景 daemon 時，不再讓 daemon 繼承 CLI 的 stdout／stderr pipe；擷取 CLI 輸出的呼叫端（agent、`subprocess.run`）在 CLI 結束後即 EOF，不再等到 daemon 結束（SC-18 autostart 在原生 Windows 卡死 3600 秒的根因）。
