> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`6ebd0d2ebfb6cb69c29ed4f0591be349f420312c`，detached、工作樹乾淨。增量只有 `6ebd0d2e`，已逐檔檢查，無 merge。未找到 `p8-fix-responses.md`；下列修正提交均為 HEAD 祖先。未受本輪影響者沿用前輪證據。

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
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 後續子項承接，不增加輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 未改。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原未改。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接與交錯案例斷言未改。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離未改。 |
| P8-591d2ff-1 | Low | 未修 | 15 | `tests/test_contracts_catalog.py:1451` 仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 明確 release／Drop 放行及清除 paused 未改。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 可追溯報告與失敗／待驗收聲明未改。 |
| P8-0896933-1 | Low | 未修 | 7 | 驗收報告統計與時間誤植未改。 |
| P8-c89cd78-1 | Low | 未修 | 5 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-26 為 `APPROVED`。 |
| P8-057a065-1 | High | 已修 8f2d661b | 0 | actor 結束時取出並回傳資源所有權未改。 |
| P8-057a065-2 | High | 已修 50e1e798 | 0 | stop_id 配對、Closing 拒收與錯配後阻擋未改；定向測試通過。 |
| P8-057a065-3 | Medium | 已修 50e1e798 | 0 | 原凍結時限案例通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 核對 `origin/docs/parity-amendments` 的 `rulings/s2.md`：panic 超限維持 actor 存活，由 IoError 停止機制收尾。 |
| P8-8f2d661-1 | High | 已修 50e1e798 | 0 | 缺通道歸還所有權、補通道後關閉及結算測試通過。 |
| P8-50e1e79-1 | Medium | 已修 03eb4d32 | 0 | 凍結前未入帳 Open、通知解凍後延遲輪詢兩案通過。 |
| P8-03eb4d3-1 | Medium | 已修 6ebd0d2e | 0 | 改由 gate 記錄實際凍結區間；原四步情境成功送達，新增回歸通過。 |

## B 新發現

**P8-6ebd0d2-1 [Medium]** — [crates/sw-core/src/custody.rs:859](crates/sw-core/src/custody.rs:859)：**輪詢時間與凍結累計不是同一時刻的快照，會扣除取樣時間之後的凍結，讓逾時義務仍送達。**

`frozen_total(now)` 的已結算部分直接回傳目前 `frozen_accum`，沒有截斷到傳入的 `now`（[admission.rs:367](crates/sw-core/src/admission.rs:367)）。呼叫端取得 `now` 後被暫停、其他執行緒完成凍結／解凍，即可形成以下交錯：

1. `t=0` 建立義務。
2. 送達執行緒於 `t=30.1s` 取得輪詢時間，隨後暫停。
3. 另一執行緒於 `t=30.2s` 凍結，`t=30.4s` 解凍。
4. 送達執行緒恢復，以已取得的 `now=30.1s` 執行 `step_delivery`；channel 有空位。

義務在凍結前已經歷 **30.2 秒 Open**，預期 `TimedOut`。新版卻計算 `30.1−0.2=29.9s`，回 `Delivered`。

以 stdin 編譯的記憶體探針，讓前版原始 `Deliverer` 與新版共用相同 gate、時間及輸入，隔離比較本次計時修改：

| 送達邏輯 | 結果 | 接收端事件數 |
|---|---|---:|
| `03eb4d32` | `TimedOut` | 0 |
| `6ebd0d2e` | `Delivered` | 1 |

**確切修法：**由 gate 在同一把鎖內取得時間、凍結狀態與累計，回傳一致快照；義務建立及期限檢查皆使用該快照。若保留任意 `now` 查詢，則必須保留足夠區間資料，正確計算截至該時刻的累計。新增上述「時間取樣後完成凍結／解凍」回歸，斷言 `TimedOut`、義務移除、接收端零事件。

本輪驗證：

- 指定 `CARGO_TARGET_DIR`：`custody` **20 passed**；`admission` **13 passed**。
- 契約檢查 `ok=true`；policy **25 passed、0 failed、2 warnings**（R-19／R-22，非 PR context）。
- 增量 `git diff --check` 通過；結束時工作樹乾淨。
- 未修改來源或文件；建置產物僅置於指定 target。未跑完整 pytest、Docker／bundle，未接觸 production daemon、指定設定／狀態目錄或實體 UART。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- P8-c4913ae-1 與 R2～R6 缺口依指定後續子項承接。
- 實機 1 MB pull、host 完整 pytest，以及 sw-pal 無 features／Clippy 限制，本輪未重驗。

## 對裁決的異議（不計入結論）

無。

結論：需修正