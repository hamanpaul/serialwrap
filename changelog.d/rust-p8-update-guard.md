---
type: fix
scope: rust-cli
---
P8（Slice 2.4）Task 14：更新／回滾守衛對 Rust daemon 的驗證與補強（SC-23）。CLI `safety_check` 的 daemon 健康判定改為三態：連線被拒或端點不存在且無 `serialwrapd` 行程才視為不存在並放行；`health.ping` 逾時、壞回應、其他連線失敗或行程存在但 RPC 不通一律視為健康不明並拒絕（`TIMEOUT`／`INVALID_RESPONSE`／`SOCKET_ERROR`／`DEVICE_STILL_HELD`，detail 帶 `health:"unknown"` 與 `reason`），`--force` 不影響。Rust daemon 新增共同 admission 邊界 `AdmissionGate` 並承接 `daemon.handoff_stop`：busy 判定、凍結新操作與發 token 原子完成，忙碌回 `FLASHING_BUSY`／`SESSION_BUSY`＋`busy[].ops`、不凍結不停止；成立後凍結背景來源與既有 bridge 的新 TX（TX fence 逾時回 `SESSION_BUSY`＋`tx.in_flight` 並只撤銷本次凍結），保留 3 秒排空窗口（有副作用請求回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`），窗口結束才進入有序停止；flash sync-probe 寫第一個 byte 前先取 admission ticket。
