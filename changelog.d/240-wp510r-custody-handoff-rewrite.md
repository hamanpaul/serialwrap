---
type: fix
scope: rust-core
---
依定稿設計第 5 版與擁有者裁決（方案 A′、D1 三態＋T-1、D2～D6），重寫 OD-63 RELEASED 佔位 session 的 `device.attach` 交接與共用的保管路徑（S2-PAR-wp510r R1～R6）：
- **保管帳本**：manager 以 by-id 帳本記錄尚未被 actor 確認接手的 handle 或 IoWorker（Opening／Held／Taken／Bridged／BridgeDraining／Closing／CloseFailed），session 生命週期一律由 actor 決定；關閉工作者、回收者、送達者分屬不同執行緒，`ExitReclaim`／`Reclaim` 守衛的 Drop 只送命令、不取 manager 鎖；actor 建構改用 `try_spawn_actor`，持鎖建構失敗在原鎖內清理、解鎖後交關閉工作者（DR-9）。回收者停止後才送交的未接手 IoWorker 退回就地收尾，不再直接丟棄。
- **關閉語意（D1）**：仍待回條時阻擋開埠，release 等首次期限；成功回條才解除；失敗或未知終態轉 CloseFailed 並永久阻擋到重啟。release 等待帳本紀錄或退休 bridge 時依 T-1 等滿首次 release 的 3 s 絕對期限才回 DRAIN_STALLED，期限已過的 release 立即回；session 自身 bridge 的排空維持立即回覆。首次期限記在被等待的對象上，之後的 release 沿用；RPC 執行緒以實時到期送帶身分的通知，只結算目前那一筆。
- **交接流程**：dynamic 偵測建立的正式 session 出生即接管 PROBE 用過的 handle（`ClaimCustody`）；adopt 提交點看到取消即關閉而不接手；同一 session 單次在途（改指時舊 bridge 回條到了才以新路徑重開）；`retired_drains` 承接所有退休 bridge，release 與 shutdown 都等其收斂；bind 一律等保管在途的終態；#181 熱插再解析改走 `BeginExternalAttach`／`CustodyOffer`；parked（退休落盤失敗保留的 handle）只由已知開埠在途的開埠工作以同一 token 認領，bind 之後首次 DeviceOnline 送不進去時由送達者以 `try_send` 加退避重送（凍結 Pending 暫停、Permanent 丟棄、總時限 30 s 扣除凍結區間）。
- **佔位 W1 改由 actor**：佔位 actor 先準備交接 worker 再以 PlaceholderRaw 模式開埠，退休在佔位 actor 執行緒上、manager 鎖內一次完成；落盤失敗依 D2 以同一 token 存回 parked；`device.attach` 帶 manager 的裝置快照與原 selector；並行 release 回應形狀相同、`already_released` 鍵序與 Python 一致；一般 OSError 收尾保留不可撤銷的裝置取消到退休決策。
- **熱插拔除 parked**：拔除時關閉 parked handle 期間，同一 by_id 的重插不另開，關閉成功後補排一次 attach；已綁到該 by_id 的 session 其 release 等這次關閉的回條（首次期限依 T-1）；逾時回 DRAIN_STALLED 之後回條才 closed:true 時，由該 session actor 保留的停止待決補完 release（RELEASED、清 `drain_stalled`／`drain_timeout_epoch`、落盤、不重開，對齊 Python `_finish_attach_op`）；shutdown 掃描關閉 parked 後以同一 token 結算帳本紀錄。
- **T14 交接凍結**：新增的事件、請求與效果依設計 §3.1.7 分類；交接凍結期間建構的出生即接管 session 不再執行 `ClaimCustody`（Pending 延後到撤銷凍結、Permanent 只關閉）。
- **flash 入口（G-6）**：by_id 在未確認關閉集合者不列為 flash 偵測候選；退休 bridge 排空中或已登記未確認關閉時，flash probe 與進 FLASHING 一律拒絕（對齊 Python `_draining_by_ids`）。
- **凍結節點**：110 個 wp510 L1 節點名稱與步驟順序全部保留，只依設計 §6 與擁有者裁決改寫 S（#3～#6、#55、#78 與表外 `attach_finish_respawns_when_state_requires`）、F（#10、#94）、H*（#1、#2）與 H 類的觸發手段或內部查詢；新增回歸以 `regen_lock.py --l1-add-only` 登記。
