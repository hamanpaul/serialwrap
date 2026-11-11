---
type: fix
scope: rust-core
---
S2-PAR-wp510r R3（設計第 5 版 §3.1.3、§3.1.6；審查 P8-566d90b-1～4）：
- `session.bind` 遇退休 bridge 尚未確認關閉時改為等待其終態（BindWait，上限 3000 ms）：成功回條後照原流程改綁；失敗回條、已知失敗或逾時回 SESSION_RECOVERING，session 設 RECOVERING、`drain_stalled:true` 與 epoch，不改綁（Python `bind_session`）。
- retired drain 等待期間收到失敗回條、或同時有 retired drain 時自身 bridge 回不安全回條，release 改依 T-1：不提前回覆、不改 drain 欄位，到首次期限才回 DRAIN_STALLED，期限已過的 release 立即回。凍結節點 #10 依設計 §6 F 類同步翻轉。
- release 的首次期限自 RPC 進入時起算（實時）；未獲回覆即通知 actor（`ReleaseDeadlineElapsed`），只作用在有 retired drain 的 T-1 等待。
- 有自身 bridge 的 session 不再因退休 bridge 未收斂而在 `device.attach`／`session.attach` 回 SESSION_RECOVERING（Python 有 bridge 的冪等判斷先於在途表分流）。
- 開埠在途的 clear、拔除、改指等停止，在新開埠失敗時照開埠終態完成，不再因退休 bridge 未收斂而永不回覆；只有 release 與 shutdown 等 retired drain。
