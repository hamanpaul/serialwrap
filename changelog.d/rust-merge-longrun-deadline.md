---
type: fix
scope: rust-refactor
issue: null
---
長跑收尾輪詢背景命令時，將單次 RPC timeout 限制在設定的總期限內，避免浮點捨入使逾時略超上限。
