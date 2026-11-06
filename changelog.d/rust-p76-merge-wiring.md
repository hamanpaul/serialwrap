---
type: fix
scope: rust-p76
---
合併 P6 與 P7 時，為 RX observer 測試補齊 P6 新增的 hub 欄位，保留既有 observer 與 WAL 順序斷言。
Python session manager 恢復 main 基線的取消語意，避免原命令 TX 完成後的取消連帶撤銷仍存活的 interactive lease。
