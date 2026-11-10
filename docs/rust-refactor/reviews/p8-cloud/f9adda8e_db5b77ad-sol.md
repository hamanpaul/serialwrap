> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

已確認修正提交皆為 HEAD `db5b77ad` 的祖先；本輪僅修改測試與 L1 鎖，未改動下列修正邏輯。

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

已逐一審查 `b91ef597`、`db5b77ad`；範圍內無 merge commit。

- **正式接線**：daemon 的 `ServiceHandler` 呼叫同一個 `Service::rpc`；來源授權經 `SubmitReq`、佇列、OpRunner 到 actor 結算與 lease。符合 OD-74、D5 與 Python 基線，沒有僅供測試使用的替代實作。
- **整體驗收**：wp61 全部 9 個定向測試通過，涵蓋可信來源升格、救援鍵抑制、不可信來源救援、來源保留、lease owner、授權不跨命令殘留，以及保留來源拒絕。新增夾帶參數案例精確檢查 UART／WAL TX、命令終態與公開欄位；既有斷言未削弱。
- **add-only 登記**：原有 49 個集合及其 selector、nodeid 完全不變。依新增 selector 建置並收集得到 1,891 個節點，其中 7 個 ignored；其餘 1,884 個與新鎖集合精確相等，無重複或遺漏，包含全部 9 個 wp61 節點。

定向驗證皆使用指定 `CARGO_TARGET_DIR`：

```bash
cargo test --locked -p sw-daemon --features test-fakes \
  --test dispatch wp61_ -- --nocapture

cargo test --locked -p sw-core --features test-fakes \
  --test command_exec --test session_core_lease wp61_ -- --nocapture
```

結果：**9 passed、0 failed、0 ignored**。

`regen_lock.py --check --base f9adda8e` 與 `git diff --check f9adda8e..db5b77ad` 通過。未回退版本或修改產品碼重做突變測試；提交訊息中的突變結果未當成本輪實測證據。

工作樹維持乾淨，未修改檔案；僅產生指定 target 內的建置產物。未跑完整 pytest、docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。此結論涵蓋 **wp61 第 4／4 項及本包接線驗收**，不代表整個 Stage 2 出口通過。

## 既有碼待辦（不計入結論）

- wp510／wp510r：承接 P8-c4913ae-1，保留逾時狀態至全部 handle 確認關閉。
- 後續 Rust daemon 對齊工作包：沿用前輪記錄，一般 RELEASED session 在 `device.attach` 開埠 OSError 後再遇落盤失敗的 Rust／Python 終態差異；本輪未改動、未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行