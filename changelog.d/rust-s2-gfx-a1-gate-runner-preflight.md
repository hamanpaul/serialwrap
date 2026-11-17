---
type: fix
scope: rust-refactor-gate
---
Stage 2 gate 正式執行前拒絕髒工作樹並列出變更；前置檢查／建置失敗會在 run log 保留原因並使用專用結束碼。P7 預設報告改寫入 `target/p7-gate/<run_id>/`，避免污染文件目錄。
