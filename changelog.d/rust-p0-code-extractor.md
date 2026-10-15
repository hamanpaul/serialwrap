---
type: feat
scope: contracts
---
新增宣告式 AST 抽取器以追蹤 `sw_core` 的 error_code 與 session last_error，涵蓋 scope 正確的預設值、重綁參數、字典查找、callback、tuple 傳遞、例外欄位與動態前綴，並以有界 memoization 保留未解析原因（OD-06）。
