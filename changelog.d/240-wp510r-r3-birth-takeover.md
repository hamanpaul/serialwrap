---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 8 項（設計 §3.1.3、§3.1.4、§3.1.6；編排者 2026-11-11 SPEC-GAP 2、3 裁決）完成 R3 其餘部分：
- 出生即接管：dynamic 偵測建立的正式 session 出生即帶 `external_attach{token}`、ATTACHING，PROBE 用過的 handle 存為該 session 擁有的帳本紀錄（`Held{Session}`），以啟動效果 `ClaimCustody` 開埠，不再送 `DeviceAttached`；交給既有 session 的預開 handle（parked 續行、bind 改綁到 parked、DETACHED-rebind、#181 再解析）改以同步的 `AcceptCustody` 先由 actor 接受再轉移歸屬（設計 C7）。
- 交接表、撤銷標記與 `ReleaseAttachPending`／`ClearRevokedHandoff` 移除：release、bind、shutdown、clear、拔除對本 session 的帳本紀錄一律經 actor 的 `ClaimForStop`（`Held` 轉 Closing 交關閉工作者，`Taken` 寫入取消），立即回饋 `CustodyClaimed`，關閉終態以 `CustodyTerminal` 回報；等待併入 retired drain 的收斂規則與 T-1。
- adopt 提交點的取消：開埠工作在 manager 鎖內看到取消即關閉 handle 而不接手（不啟動 IoWorker、零 TX），回 `OpenFailure::Cancelled`。
- release／bind 的期限（SPEC-GAP 2）：actor 受理等待時以 `WaitAck` 回報被等待對象的身分與首次期限，RPC 執行緒以實時到期送帶同一身分的通知；首次期限記在被等待的對象（retired drain、帳本紀錄）上，之後的 release 沿用；過期或重複的通知不產生效果。bind 等待期間有 release 加入時，終態到時回 SESSION_RECOVERING 而不改綁。
- 首次 release 加入在途等待時設 RECOVERING 並清 `drain_stalled`／`drain_timeout_epoch`（G-3）；經帳本紀錄收尾的 release 兩欄都清（SPEC-GAP 3）。
- 凍結節點依設計 §6 改寫 `drain_release` #1、#2（H*）、#3、#4（S）；套件內兩個通知測試改以 `ReleaseWithAck` 取得通知身分。
