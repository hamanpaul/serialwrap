---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r R1 審查意見修復保管帳本（CustodyLedger）與協同機制之 High/Medium 缺失：
- P8-057a065-1 [High]：`handle_actor_exited` 取出受保管之 handle parts 與 worker，產出具所有權之 `ActorExitedJob` 轉交關閉工作者與排空，不遺失資源；`Bridged{worker: None}` 保留給 Reclaim 處理。
- P8-057a065-2 [High]：`settle_worker_stopped` 嚴格核驗 phase、`receipt.epoch` 與身分配對，拒絕過期回條與錯配回條，維持開埠阻擋直到合法憑證結算。
- P8-057a065-3 [Medium]：`Deliverer` 改以累計 Open 狀態耗時（`open_elapsed`），排除 `HandoffFreezeState::Pending` 凍結期間耗時，落實 Pending 期間不計入 30 秒總時限（C4）。
- P8-057a065-4 [Medium]：在單元測試中使用正式 `try_spawn_actor_with_reaper` 啟動 actor，完整覆蓋正常退出、panic unwind、panic 超限與注入 spawn 失敗四種路徑。
