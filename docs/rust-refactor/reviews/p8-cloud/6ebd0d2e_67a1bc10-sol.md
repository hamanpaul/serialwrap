> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`67a1bc108f6a0e2609b5c3e30d50b6abc0751196`，detached，工作樹乾淨。本輪只有 `67a1bc10`，已逐檔檢查，無 merge。未找到 `p8-fix-responses.md`；下列修正提交均為 HEAD 祖先。未受本輪影響者沿用前輪證據。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞未改。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消互斥未改。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對未改。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉阻擋與持久化重試未改。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗排空路徑未改。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | `finish_attach_op` 鎖內重讀未改。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 重試認領並關閉 parked 作業未改。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用關閉作業與首次期限未改。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成與持久化路徑未改。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態未改。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | release 等待退休 bridge 關閉回條未改。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 後續子項承接，維持輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 未改。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原未改。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接與交錯案例斷言未改。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離未改。 |
| P8-591d2ff-1 | Low | 未修 | 16 | `tests/test_contracts_catalog.py:1451` 仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 明確 release／Drop 放行及清除 paused 未改。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 可追溯報告與失敗／待驗收聲明未改。 |
| P8-0896933-1 | Low | 未修 | 8 | 驗收報告統計與時間誤植未改。 |
| P8-c89cd78-1 | Low | 未修 | 6 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-26 為 `APPROVED`。 |
| P8-057a065-1 | High | 已修 8f2d661b | 0 | actor 結束時取出並回傳資源所有權未改；定向測試通過。 |
| P8-057a065-2 | High | 已修 50e1e798 | 0 | stop_id 配對與錯配後阻擋未改；定向測試通過。 |
| P8-057a065-3 | Medium | 已修 50e1e798 | 0 | 原凍結時限案例通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對 `origin/docs/parity-amendments` 的 `rulings/s2.md`；panic 超限維持 actor 存活，由停止機制收尾。 |
| P8-8f2d661-1 | High | 已修 50e1e798 | 0 | 缺通道歸還所有權及補通道後結算路徑未改。 |
| P8-50e1e79-1 | Medium | 已修 03eb4d32 | 0 | 凍結前 Open、解凍後延遲輪詢案例通過。 |
| P8-03eb4d3-1 | Medium | 已修 6ebd0d2e | 0 | gate 記錄實際凍結區間的計時語意保留；相關案例通過。 |
| P8-6ebd0d2-1 | Medium | 已修 67a1bc10 | 0 | 歷史區間正確截斷至取樣時刻；原交錯實測回 `TimedOut`、義務移除、零事件。 |

前輪問題的獨立重播：從兩個 commit 讀取原始 `admission.rs`、`custody.rs`，經 stdin 編譯，以相同記憶體輸入執行：

| 版本 | 結果 | 剩餘義務 | 接收事件 |
|---|---|---:|---:|
| `6ebd0d2e` | `Delivered` | 0 | 1 |
| `67a1bc10` | `TimedOut` | 0 | 0 |

新增回歸位於 [custody.rs:893](crates/sw-core/tests/custody.rs:893)，明確斷言上述三項結果。

## B 新發現

NO FINDINGS。

本輪驗證：

- `cargo test --locked --offline -p sw-core --features test-fakes --test custody --test admission`：**22＋14 passed，0 failed**。
- 契約檢查：`ok=true`。
- Policy：**25 passed、0 failed、2 warnings**（R-19／R-22；非 PR context）。
- 增量 `git diff --check` 通過；結束時 HEAD 未變、工作樹乾淨。

未修改來源或文件；建置與探針產物均置於指定 `/tmp/solrev-p8-rust-refactor-p8-target`。未跑完整 pytest、Docker／bundle，未操作 production daemon、指定設定／狀態目錄或實體 UART。本結論限於 wp510r 第 3／8 項。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- P8-c4913ae-1 與 R2～R6 缺口依指定後續子項承接。
- 實機 1 MB pull、host 完整 pytest，以及 sw-pal 無 features／Clippy 限制，本輪未重驗。

## 對裁決的異議（不計入結論）

無。

結論：可放行