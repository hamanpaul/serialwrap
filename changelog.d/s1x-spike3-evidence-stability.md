---
type: fix
scope: rust-refactor
---

穩定 Stage 1A Spike 3 的 Windows helper 證據：ready 記錄以同目錄暫存檔原子發布，consumer 對未完成記錄重試；四種主案例都先確認 helper 已持有現行 WAL 再注入操作。helper 啟動失敗仍輸出符合 schema 的 INSUFFICIENT_EVIDENCE，保留可診斷證據。
