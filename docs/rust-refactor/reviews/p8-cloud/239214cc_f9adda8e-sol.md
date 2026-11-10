> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

已確認下列修正提交均為 HEAD `f9adda8e` 的祖先；本輪未改動其修正邏輯，沿用前輪證據並核對增量差異。

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
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 本輪未改動；由 wp510／wp510r 承接，不增加未修輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | 交接判定的 atomic 鏡像與鎖內 TX fence 保留。 |

## B 新發現

NO FINDINGS。

已逐一審查 `5a271835`、`f9adda8e`；範圍內無 merge commit。

- `OpRunner` 逾時改用 `is_trusted_human()`，與 OD-74、D5 及 actor 結算判定一致。可信 human 不送 CTRL-C／CTRL-D；不可信 `human:<x>` 仍走兩階段救援，符合 Python `_recover_after_failure`。
- 新增矩陣涵蓋六種情境，核對 UART／WAL TX、命令終態、`recovery_action` 與 lease owner；既有斷言未削弱。
- L1 鎖三個集合各新增一個 nodeid；無刪除、重複、既有順序或其他值變動。

定向驗證：

```bash
CARGO_TARGET_DIR=/tmp/solrev-p8-rust-refactor-p8-target \
cargo test --locked -p sw-core --features test-fakes \
  --test command_exec -- --nocapture
```

結果：**113 passed、0 failed、0 ignored**。未回退執行修正前版本；紅燈有效性由舊分支會送 CTRL-C、而新案例精確斷言零救援鍵的程式路徑核對。

`git diff --check 239214cc..f9adda8e` 通過，工作樹維持乾淨。未修改檔案；僅產生指定 target 內的建置產物。未跑完整 pytest、docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。本結論僅涵蓋 **wp61 第 3／4 項**。

## 既有碼待辦（不計入結論）

- wp61 第 4 項：接線與整體驗收仍待完成；本輪 ActorRig 證據不代表正式 RPC 端到端驗收。
- wp510／wp510r：承接 P8-c4913ae-1，保留逾時狀態至全部 handle 確認關閉。
- 後續 Rust daemon 對齊工作包：沿用前輪記錄，一般 RELEASED session 在 `device.attach` 開埠 OSError 後再遇落盤失敗的 Rust／Python 終態差異；本輪未改動、未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行