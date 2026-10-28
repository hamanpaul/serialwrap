---
type: fix
scope: rust-refactor
---

OD-25：`scripts/rust/win-startup-100.ps1` 改為只做隔離環境——100 次 CLI 執行的子行程 TEMP／TMP／LOCALAPPDATA 指向本次專用的空隔離目錄，隔離目錄內任何新增（不限副檔名）即 FAIL；exe 目錄新增目錄／exe／dll 仍 FAIL；真實 `%LOCALAPPDATA%`／`%TEMP%` 的新增只記於 `ambient_changes`，不再因其他桌面程式的目錄誤報。
