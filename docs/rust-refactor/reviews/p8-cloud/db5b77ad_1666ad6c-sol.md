> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

已確認下列修正提交皆為 HEAD `1666ad6c` 的祖先；相關產品碼相較前輪 `db5b77ad` 完全未變。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消的鎖內互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉的阻擋與持久化重試保留。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗仍可完成排空。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業的路徑保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化路徑保留。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 wp510／wp510r 承接，不增加未修輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | 交接判定的 atomic 鏡像與鎖內 TX fence 保留。 |

## B 新發現

NO FINDINGS。

已逐筆審查範圍內唯一提交 `1666ad6c`，無 merge commit。

- 依 `origin/docs/parity-amendments@7404f46a` 的 `reviews/rulings/s2.md:71`（OD-81），本項調整符合裁決。
- `timeout_s=60` 經 `P7Rig.start()` 傳入暫存 profile，再交給 daemon；未指定時仍保留來源值，來源 YAML 未變。
- 800 ms 回應延遲、50 ms 雜訊、180 秒輪詢上限，以及 READY、`last_error`、登入 TX 斷言全部保留；沒有刪測試、放寬斷言或改動凍結鎖。

定向實測：

| 驗證 | 結果 |
|---|---|
| 新增覆寫自測＋既有代換自測＋Python 延遲雜訊黑箱 | **3 passed**，63.98 秒 |
| 本 HEAD 建置的 Rust CLI／daemon 延遲雜訊黑箱 | **1 passed**，63.53 秒 |
| 記憶體內換用 `db5b77ad` helper 執行新增自測 | 如預期失敗，確認可攔截未修版本 |

Rust 建置使用指定 `CARGO_TARGET_DIR`；pytest 使用 `--confcutdir=tests/contract_blackbox`，停用 bytecode 與 cache。`git diff --check` 僅報自測檔第 251 行新增 EOF 空白行，屬不擋放行的格式問題。

未修改來源檔，結束時工作樹乾淨；未跑完整 pytest、docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。本結論僅涵蓋 **b6 第 1／5 項**。

## 既有碼待辦（不計入結論）

- wp510／wp510r：承接 P8-c4913ae-1，保留逾時狀態至全部 handle 確認關閉。
- b6 後續子項：DF-27 確定性雙軌黑箱、Rust 退避重探、契約與節點登記、整體接線驗收。
- 沿用前輪待辦：一般 RELEASED session 在 `device.attach` 開埠 OSError 後再遇落盤失敗的 Rust／Python 終態差異；本輪未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行