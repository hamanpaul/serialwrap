---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 5 項審查意見完成真實資源遷移修正與正式路徑端到端驗收（P8-bb1a899-1、P8-bb1a899-3、P8-7d19d5c-1）：
- 健全背景執行緒生命週期管理（P8-7d19d5c-1）：ReaperThread 與 DelivererThread 由 SessionManager 持有並於 drop/shutdown 時停止，回呼採用弱引用解除 Arc 強循環。
- 完善真實資源遷移與交接路徑（P8-bb1a899-1）：修正 spawn_attach_locked 認領 parked handle 走 W2 接手，attach_dynamic 自帳本取出資源原子推進為 Taken 狀態，解決落盤失敗後沿用預開 handle 之端到端閉環。
- 補齊動態交接探針與正式路徑驗證（P8-bb1a899-3）：驗證佔位 attach 後新 actor 建構失敗之關閉結算（closed:true 結算為 None、closed:false 標記 CloseFailed 並記錄至 unconfirmed）、DR-1 真實 IoWorker 背景排空結算及 DR-7 送達重試。
