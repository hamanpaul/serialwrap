> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`a94151ac`。本輪僅一個提交，無 merge。下列修正提交皆為 HEAD 祖先，相關修正檔案本輪均未變更。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消互斥保留。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉的阻擋與持久化重試保留。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑保留。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀 `close_failed` 保留。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限保留。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成 RELEASED 與持久化路徑保留。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條保留。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 wp510r 承接，不增加輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 保留。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原邏輯保留。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接及交錯案例斷言保留。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離保留。 |
| P8-591d2ff-1 | Low | 未修 | 3 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-27 為 `APPROVED`；維持 low-backlog。 |

## B 新發現

**P8-a94151a-1 [Medium] [crates/sw-pal/src/fakes/wal_fs.rs:432](crates/sw-pal/src/fakes/wal_fs.rs:432) — 暫停逾時被靜默當成成功，無法保證執行器停留在指定位置。**

`notify_paused_and_wait_release()` 在 15 秒後回傳 `false`，呼叫端忽略結果，繼續回傳 `Ok(())`；`paused` 仍為 `true`。

具體重現：設定暫停點、背景寫入 `b"tx\n"`，確認 `wait_paused()` 成功後，持續保留 handle 且不呼叫 `release()`。預期寫入保持暫停；實際約 15 秒後成功返回，再呼叫 `wait_paused(Duration::ZERO)` 仍得到 `true`：

```text
without release/drop: result=Ok(Ok(())), elapsed=15.000172589s, wait_paused=true
test pause_must_not_succeed_without_release ... FAILED
```

因此控制執行緒若延遲超過 15 秒，後續取消可能已落在執行器返回之後，卻仍被當成命中 DF-25 指定窗口。這是本輪新增暫停機制本身的缺陷。

**確切修法：**寫入端等待至明確 `release()` 或 handle `Drop` 才放行，保留控制端 `wait_paused(timeout)` 的有界等待。若需要 watchdog，逾時必須明確使測試失敗，不得成功續行。補上未放行超過原 15 秒期限的回歸斷言。

定向驗證：

- `cargo test --locked --offline -p sw-pal --features test-fakes --test fakes_selftest wal_fs_ -- --nocapture`：**5 passed**。
- 額外暫停期限驗證：**1 failed**，如上；測試原始碼由 stdin 編譯，產物位於指定 `/tmp/solrev-p8-rust-refactor-p8-target`。
- `git diff --check cdae01df..a94151ac`：通過。
- 未編輯檔案；工作樹乾淨。未跑完整 pytest、docker／bundle，未接觸 production daemon。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- DF-25 的 ServiceRig 驗證、conditional／L1 登記及整體接線驗收，由後續子項承接。
- P8-c4913ae-1 歸 wp510r；od79、od80 維持各自工作包範圍。

## 對裁決的異議（不計入結論）

無。

結論：需修正