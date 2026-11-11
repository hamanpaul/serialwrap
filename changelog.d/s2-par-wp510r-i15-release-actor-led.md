---
type: fix
scope: rust-refactor
---

- S2-PAR-wp510r 第 15 項（擁有者 2026-11-11 裁決）：`device.release` 等待 parked、帳本紀錄或在途 attach 作業關閉的流程改由 session actor 主導。actor 以 `ClaimByIdForRelease` 在 manager 鎖內認領 by_id 上的紀錄（parked 轉 Closing、在途作業寫入取消、別人的 Closing 只加入 watcher），首次期限記在被等待的對象上並由後續 release 沿用；關閉終態以 `CustodyTerminal` 通知（送不進去時由 `CustodyPoll` 自帳本補收），actor 收齊終態後才 RELEASED 並落盤，失敗或未知終態依 D1／T-1 於首次期限回 `DRAIN_STALLED` 並維持阻擋。DRAIN_STALLED／RELEASED／already_released 的回覆一律由 actor 依自身狀態組成；manager 層的期限、session 快照、遲到 release 補送與相關測試鉤子全部移除，actor 暫停期間 release 不再由 manager 代答。release 自己觸發的 parked 關閉失敗改為等到首次期限才回 `DRAIN_STALLED`（已知差異 N-14）。
