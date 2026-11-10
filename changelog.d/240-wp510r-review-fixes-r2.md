---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r R1 審查修正保管帳本（CustodyLedger）與協同機制之 2 High、2 Medium 缺失：
- P8-8f2d661-1 [High]：`execute_and_settle` 缺少回收通道時回傳錯誤並完整歸還工作所有權（`JobExecutionError::MissingReaperChannel`），禁止消耗 worker；補齊真 IoWorker 排空與通道缺失回歸。
- P8-057a065-2 [High]：`BridgeDraining` 登記 stop_id 並與 epoch、token 於結算時三者完整比對，拒絕 `StopIdMismatch` 錯配回條；`Closing` 拒絕 `WorkerStopped` 結算（`InvalidPhase`），維持開埠阻擋。
- P8-057a065-3 [Medium]：`Deliverer` 依凍結／解凍轉態記錄暫停區間並扣除，解凍回到 Open 時重設 `last_open_at`，杜絕 Pending 期間無輪詢時被誤計入 30 秒總時限；補齊「最後一次 Pending 呼叫在凍結起點」之回歸測試。
- P8-057a065-4 [Medium]：查清 `actor.rs` 在事件連續 panic 3 次時呼叫 `begin_stop(StopKind::IoError)`，不結束 actor loop 且不退出執行緒；更新單元測試斷言 panic 超限不退出且第四次請求成功，以 Shutdown 清理退出並收到恰好一筆 `ActorExited`。
