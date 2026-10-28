---
type: change
scope: wal
---

OD-19（2026-10-23）端點防毒驗收改為與防毒產品無關（S1X）：Windows WAL 的 rotate 與 unlink 遇共用違規（ERROR_SHARING_VIOLATION）或存取拒絕（ERROR_ACCESS_DENIED）時改為有上限的退避重試（10／50／200 ms），仍失敗者進入延遲佇列（1 s 間隔、非阻塞，於後續 append 或 `tick()` 重試直到完成），寫入路徑不持鎖睡眠且不因輪替阻塞；`health.status` 的 `wal` 新增 `rotate_retry`、`rotate_deferred`、`deferred_delete_pending` 三個整數觀測欄位（不改 `healthy`）。新增 `scripts/stage0/av_sim.py` 模擬防毒持有（不含 FILE_SHARE_DELETE、seed 可重現、10～500 ms）作為可重現的故障注入；spike3 改以 av_sim 注入與 `binary_integrity`（執行檔前後存在、sha256 不變、啟動未被拒）判定，防毒產品名與狀態改為工具唯讀查詢的資訊欄位，`--edr-*` 自填參數與 `SW_EDR_*` 已移除，`stage1a_gate` 的 spike3 判準隨之改為 binary_integrity。

接線與收緊（2026-10-23 審查修正）：`SerialwrapService` 的 DeviceWatcher 週期回呼改為先 `WalWriter.tick()` 再 `reconcile_readiness()`，使延後輪替在無新寫入時仍會完成；tick 驅動的現行檔輪替完成時寫入 `WAL_ROTATED_AFTER_DEFERRAL` META（維持現行檔存在與重啟後 seq 續接）。`AdmissionGate` 僅撤銷由交接自身建立的凍結。spike3 gate 收緊為逐 subject 重算 sha256、要求 `grabs ≥ 1`、`launch_error == none`，且 oplock 變體未實作（`not_implemented`）時不得 PASS（`oplock_not_covered`）。producer 現記錄每次 `WalWriter.append` 毫秒耗時；缺少有效樣本不得 PASS，上限未經 owner 裁決時維持 BLOCKED-OWNER。WAL 輪替持續 deferred 時每 60 秒最多再告警一次。
