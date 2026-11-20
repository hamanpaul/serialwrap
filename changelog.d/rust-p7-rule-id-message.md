---
type: fix
scope: rust-p7
---
`sw_core::event_engine::schema`：`rule_id must equal '…'` 錯誤訊息改依 Python f-string 直接嵌入衍生 id（不再 repr 跳脫），名稱尾端換行等字元與 Python 逐字一致（sol 補審 #3）。
