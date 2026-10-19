---
type: feat
scope: rust-cli
issue: null
---
Rust CLI 原生承接 `service`（start／stop／restart／status）與 `supervision-mode` 查詢，不再委派 Python；`config.yaml` 寫入改為位元組相容的 PyYAML 輸出，壞 YAML 改為明確錯誤（DF-07）。系統唯讀設定檔不會被寫入。
