---
type: refactor
scope: rust-core
---
依據定稿設計第 5 版與擁有者裁決（OD-63 佔位交接重寫），完成 S2-PAR-wp510r R2 現行結構遷入保管帳本（Custody Ledger）與 D1（三態＋T-1）同步生效：
- 將 AttachOp 的 handle 欄位、parked、handoff 表、撤銷標記與 SessionIo::preopened 保管逐步由 CustodyLedger 承接。
- 依 D1 關閉語意三態落實（i）仍待回條阻擋開埠、（ii）成功回條解除阻擋並移除紀錄、（iii）失敗終態轉 CloseFailed 並永久登記未確認關閉。
- 依 T-1 語意調整 release 等待時點：開埠工作回 CloseUnconfirmed 時等待滿 3 秒首次期限才回覆 DRAIN_STALLED，已過期之 release 立即回覆；session 自身 bridge 與過渡期 retired drain 維持現狀。
- 翻轉 F 類凍結節點 #94（wp510_review8_rpc_bind_retry_parked_close_unconfirmed_keeps_block），並改寫 H 類凍結節點內部查詢。
- 修復審查發現 B-1、B-2、B-3：正式接通保管帳本 AdoptBridge 認領移除與 release 結算路徑，修正開埠關閉失敗過早判定 stalled 破壞 T-1 首次期限問題，並補齊 manager/actor 正式路徑注入回歸。
- 新增 DR-1(f2)～(f8)、DR-7(l)(m)、DR-9(b)(c) 確定性失敗情境回歸測試。
