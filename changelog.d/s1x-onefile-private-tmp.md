---
type: fix
scope: rust-refactor
---

`check_no_onefile_extract.py` 新增 `--private-tmp`（Linux）：於 user+mount namespace 內以 bind 進來的全新空目錄作為獨占 /tmp 執行，其中任何建立或殘留都判 FAIL（含寫死 /tmp 的解壓器），不再因共用 /tmp 的他人事件恆判證據不足；無法隔離時判 INSUFFICIENT。Stage 1 exit driver 改用此模式。
