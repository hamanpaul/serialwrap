> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

本輪僅新增文件；相關程式與測試均未變更。下列已修項沿用前輪驗證，並確認修正提交皆為本次 HEAD 的祖先。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞路徑未變。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消的鎖內互斥未變。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對與鎖內通知未變。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉阻擋與持久化重試路徑未變。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑未變。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 未變。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業的路徑未變。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限未變。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到 RELEASED 與持久化路徑未變。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態的處理未變。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 配對關閉回條的處理未變。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 2 | 程式仍未修：`session_core.rs:4135` 先清除 `drain_stalled`，再等待退休 bridge。依本次範圍歸屬 `wp510`，不阻擋 syncmain。 |

## B 新發現

NO FINDINGS。

已逐一核對範圍內唯一提交 `bd1b56da`，僅新增 `docs/rust-refactor/reviews/s2-main-python-parity.md`，共 21 行。

- 合併前 `c4913aed` 與合併後 `3864e9b5` 的 `sw_core/` tree 均為 `a9ceb49e324eca4068ff0ecfd4a41cbac9efd498`；共同祖先 `1fa7b785` 到 main 端 `47097715` 的 Python runtime 差異也為空，支持「本次同步無新增 Python daemon 行為需移植」。
- DF-25 的正式 `command.cancel` → `CancelQueued` → 命令 op 撤銷路徑與 lease send 接線符合文件描述；引用的回歸測試可追溯至既有提交 `b18f62fb`。
- 定向驗證：`cargo test --locked --offline -p sw-daemon --features test-fakes --test dispatch wp58_g12_ -- --nocapture`，**2 passed、0 failed**，涵蓋提交前取消零 TX，以及取消原命令後同一 lease 連續兩次送出成功。

建置產物位於指定 `/tmp/solrev-p8-rust-refactor-p8-target`。未修改原始碼、測試或文件；工作樹乾淨。未跑完整 pytest、docker／bundle，未接觸 production daemon、正式設定／狀態或實體 UART。

## 既有碼待辦（不計入結論）

- `wp510`：`P8-c4913ae-1` 仍須保留逾時狀態至全部 handle 確認關閉；本輪僅核對程式未變，未重跑前輪補充重現。
- 後續 Rust daemon 對齊工作包：延續前輪的一般 RELEASED session 在 `device.attach` 開埠 OSError 後再遇落盤失敗時，Rust／Python 終態差異。本輪未改該路徑、未重新實測。

## 對裁決的異議（不計入結論）

無。

結論：可放行