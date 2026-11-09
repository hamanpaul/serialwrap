> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

已確認下列修正提交皆為 `1533cb54` 的祖先，並核對合併後路徑與定向回歸。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留，RPC 回歸通過。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消的鎖內互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留，遲到收尾回歸通過。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉阻擋與持久化重試回歸通過。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗可完成排空。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業的回歸通過。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化回歸通過。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態，相關回歸通過。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條的回歸通過。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 仍於 `session_core.rs:4449` 提前清除 `drain_stalled`；依指定範圍歸屬 `wp510`。 |

## B 新發現

**P8-1533cb5-1 [High]** `crates/sw-core/src/session_core.rs:3937` — **交接狀態檢查阻塞於 TX fence，破壞交接拒絕回應。**

本輪合入的事件分派在處理每個事件前呼叫 `handoff_freeze_state()`（`:842–843`）；admission 尚未凍結時，該函式以阻塞方式取得 TX fence。

具體情境：READY 的 COM0 有背景寫入停在持鎖的 OS submit 階段，此時呼叫 `daemon.handoff_stop({"reason":"update"})`。

- **預期**：依 Python 基線與既有回歸，回 `SESSION_BUSY`，`busy[].ops` 包含 `tx.in_flight`，交接不成立。
- **實際**：actor 連 `GetState` 都被 fence 阻塞，回覆變成 `state:"UNKNOWN"`、`ops:["session.get_state"]`；尚未執行 TX fence 收斂檢查便返回。
- **重現**：`handoff_tx_fence_timeout_thaws_without_stop` 與 `auto_reprobe_ready_result_survives_failed_handoff_tx_fence` 均失敗，分別以 `--exact --test-threads=1` 單獨重跑仍失敗。Python 對照為 `tests/test_stage0_handoff_stop.py:372`。
- **確切修法**：讓交接生命週期查詢不依賴 OS submit 持有的 mutex；由 admission 狀態及獨立同步的凍結旗標判定，保留 Pending／Permanent 與回滾語意。唯讀查詢須能完成，再由有期限的 `freeze_for_handoff` 判定在途 TX；上述兩項測試應維持原斷言並通過。

驗證結果：

- 前輪相關定向測試：`session_manager wp510` **42 passed**、`dispatch wp510` **48 passed**、`drain_release wp510` **15 passed**。
- `handoff_stop` 整檔已觀察 **18 項通過、2 項失敗**；另有 200 輪競態測試未完成，由我終止，未計為通過。
- 案例鎖與 L1 鎖未刪除任一 parent 的必要項目；`check_p8_prereqs.py --strict` 通過。
- 未修改檔案；工作樹乾淨。建置使用指定 `/tmp` 目錄；未跑完整 pytest、docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。

## 既有碼待辦（不計入結論）

- `wp510`：P8-c4913ae-1，保留逾時狀態至全部 handle 確認關閉。
- 後續 Rust daemon 對齊工作包：一般 RELEASED session 在 `device.attach` 開埠 OSError 後再遇落盤失敗的 Rust／Python 終態差異；本輪未修改該路徑，未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：需修正