---
type: fix
scope: rust-refactor
---

`check_no_onefile_extract.py` 新增 `--private-tmp`（Linux）：於 user+mount namespace 內以 bind 進來的全新空目錄作為獨占 /tmp 執行，其中任何建立或殘留都判 FAIL（含寫死 /tmp 的解壓器），不再因共用 /tmp 的他人事件恆判證據不足；無法隔離時判 INSUFFICIENT。直譯器、本工具或受測 CLI（含 /tmp 下的 symlink）位於 /tmp 下時一律以用法錯誤 exit 2 拒絕。Stage 1 exit driver 改用此模式，並核對報告新鮮度與 `verdict PASS`＋`tmp_isolation`。
