---
type: feat
scope: daemon
---

新增 `SERIALWRAP_CONFIG` 與系統設定檔唯讀 fallback，正規化 lock／socket 路徑並讓 Windows singleton lock 使用 `LockFileEx`。監管切換與 `daemon restart` 會先確認舊 daemon 已結束；daemon 啟動時先掃描裝置、配好 COM rank，再啟動 watcher。
