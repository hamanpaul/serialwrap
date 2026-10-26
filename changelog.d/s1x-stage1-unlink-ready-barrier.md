---
type: fix
scope: rust-refactor
---

修正 Spike 3 unlink 案的 helper 啟動競態：ready 改為首次目錄掃描後回報已持有檔案，unlink 前先確認 helper 實際持有現行 WAL；避免在注入尚未生效時誤判干擾未發生。
