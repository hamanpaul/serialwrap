---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 6 項（第 5 項審查 sol-7d19d5c8_6a679fb0）修正 parked 續行的帳本認領、送達者的交接閘門與 DR-7 正式路徑驗收：
- parked 續行（`spawn_attach_with`、`session.bind` 改綁目標）改以 C1′ `claim_parked` 同 token 認領（`Taken`，不再借用 `claim_for_release` 標成 `Closing{release}`）；handle 帶著帳本 token 交給交接列或 attach 作業，正式 bridge 接手時移除紀錄，於開埠前、取消者代為關閉或作業自行關閉且 closed:true 時依 C3 結算為 Closed（P8-6a679fb-1）。刪除只被人造狀態走到的兩處 `claim_parked` 接線與 `custody_register_parked_for_test`。
- `claim_for_release` 先檢查 token 與 phase，成立才改寫 owner、cancel 與 phase；非 `Held` 時回錯誤並保留紀錄原樣。
- 送達者改用命令登錄表的正式 admission gate（與 `Service::handoff_stop`、session actor 交接延後同一物件）：正式交接凍結 Pending 時暫停送達、凍結區間不計入 30 s 總時限，Permanent 時丟棄義務。
- DR-7 測試改由正式流程（佔位 attach 加退休落盤 ENOSPC）建立 parked，以完整 sid 灌滿 control lane、令 actor 停在無關 Request 使 DeviceOnline 真的入列逾時，斷言送達重試、容量恢復後只送達一次、原 token、開埠次數不增加與 READY（P8-bb1a899-3）。
- 測試穩定性：executor unwind 鉤子改為先取出再於鎖外呼叫並以 sid 限定（P8-6a679fb-2）；送達失敗收尾的兩個 session_manager 測試改輪詢至帳本終態（P8-6a679fb-3）。
