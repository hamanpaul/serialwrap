> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`b5bdca56`。已逐一檢查 `591d2ff5..b5bdca56` 三個提交，無 merge。下列修正提交皆為 HEAD 祖先；本輪未變更其實作或既有回歸斷言。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消的鎖內互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉的阻擋與持久化重試保留。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑保留。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業的路徑保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化路徑保留。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條保留。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 wp510r 承接，不增加輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 保留。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原邏輯保留。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接及交錯案例斷言保留。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與 `CARGO_TARGET_DIR` 隔離保留。 |
| P8-591d2ff-1 | Low | 未修 | 1 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-27 為 `APPROVED`；維持 low-backlog，不擋放行。 |

## B 新發現

NO FINDINGS。

本輪核對結果：

- `08755ddb` 新增測試走正式 `Service → SessionManager → Login FSM → drain → tick 重探` 路徑，驗證逾時、拒絕零副作用、再次失敗退避、恢復 READY，以及恢復後命令實際送出。
- `4ced50f3` 僅向兩個既有 L1 集合各追加該測試節點，selector 與原有節點均未變。
- `b5bdca56` 符合 OD-76：四份修訂鏈僅追加紀錄，凍結本體、分母與分母指紋不變；新 provenance 雜湊均符合現行檔案。
- 定向測試 **9 passed**：新增 RPC 測試 1 個、DF-27 核心測試 4 個、凍結／指紋測試 4 個。
- `tools.contracts check`、`regen_lock.py --check --base 591d2ff5`、`git diff --check 591d2ff5..b5bdca56` 均通過。

b6 的接線與驗收測試已納入提交；本輪未重跑完整 pytest、daemon 黑箱、docker／bundle，完整 host 驗收仍由編排者負責。未修改檔案，建置產物使用指定 `/tmp` 目錄，結束時工作樹乾淨。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- P8-c4913ae-1 由 wp510r 承接；wp73、df25、od79、od80 依指定後續工作包處理。

## 對裁決的異議（不計入結論）

無。

結論：可放行