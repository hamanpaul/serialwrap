> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`7d19d5c8b52d976eb0e05601585fdea86cbd529d`，detached，工作樹乾淨。本輪僅一筆提交，11 個變更檔均已檢查，無 merge；未找到 `p8-fix-responses.md`。下列既有修正提交皆為 HEAD 祖先；未受本輪影響者沿用前輪證據。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 退休與 release 取消共用鎖；定向回歸通過。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉仍阻擋；持久化重試案例通過。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗收尾案例通過。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | 鎖內重讀 `close_failed` 回歸通過。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 認領 parked 後關閉的路徑保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | 雙 release 共用首次期限，定向案例通過。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成案例通過。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | retired bridge 回條等待保留；時點調整由 R3 承接。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 R3 承接，維持輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與 TX fence 保留；零 TX 回歸通過。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原未改。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接與交錯斷言未改。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離未改。 |
| P8-591d2ff-1 | Low | 未修 | 19 | `test_contracts_catalog.py:1451` 仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 明確 release／Drop 放行及清除 paused 未改。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 可追溯報告與失敗／待驗收聲明保留。 |
| P8-0896933-1 | Low | 未修 | 11 | 驗收報告統計與時間欄位未修。 |
| P8-c89cd78-1 | Low | 未修 | 9 | `test_contracts_catalog.py:1439` 仍固定要求 DF-26 為 `APPROVED`。 |
| P8-057a065-1 | High | 已修 8f2d661b | 0 | 帳本單元層資源認領案例通過；正式收尾仍見 P8-bb1a899-1。 |
| P8-057a065-2 | High | 已修 50e1e798 | 0 | stop_id 配對與錯配阻擋案例通過。 |
| P8-057a065-3 | Medium | 已修 50e1e798 | 0 | 凍結時限案例通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對 `rulings/s2.md`；不要求 panic 超限退出。 |
| P8-8f2d661-1 | High | 已修 50e1e798 | 0 | 缺通道歸還所有權及補通道結算案例通過。 |
| P8-50e1e79-1 | Medium | 已修 03eb4d32 | 0 | 解凍後延遲輪詢案例通過。 |
| P8-03eb4d3-1 | Medium | 已修 6ebd0d2e | 0 | 實際凍結區間計時案例通過。 |
| P8-6ebd0d2-1 | Medium | 已修 67a1bc10 | 0 | 歷史區間截斷至取樣時刻的案例通過。 |
| P8-bb1a899-1 | High | 未修 | 2 | 真實資源已移入帳本、建構失敗已修；BridgeOpened 送達失敗仍丟失關閉終態，詳下。 |
| P8-bb1a899-2 | High | 已修 9ee1f3e2 | 0 | `close_failed`／`stalled` 分離保留；首次期限回歸通過。 |
| P8-bb1a899-3 | Medium | 未修 | 2 | 建構失敗案例有效，但 panic、DR-1、DR-7 正式路徑驗證仍不足，詳下。 |

**P8-bb1a899-1：部分修正，尚不能結案。**

前輪動態建構失敗缺口已修，新增成功關閉／關閉失敗兩案均通過。但 [session_manager.rs:3831](crates/sw-core/src/session_manager.rs:3831) 在 `BridgeOpened` 送達失敗時，取出 worker 後立即 `remove_bridged`，再把排空回條送到**已斷線的 actor 通道**，沒有交回收者結算。

獨立探針經正式 manager 路徑重播：

1. 佔位 attach，暫停於正式 bridge 已取出預開 handle。
2. 注入 executor 回呼 unwind，使 actor 在事件處理的 `catch_unwind` 外退出。
3. 令假 PAL 關閉回報失敗，放行開埠工作，觸發 `BridgeOpened` 送達失敗。

實際結果：

```text
close_count = 1
is_open = true
blocked = false
custody = None
```

預期應保留 `CloseFailed` 並登記未確認關閉；探針的阻擋斷言失敗。這是原 finding 的 R2 故障收尾缺口，不另編新號。

修法：送達失敗時保留 token，轉為 `BridgeDraining`，將真實 worker 及配對資訊交回收者；取得回條後才移除或轉 `CloseFailed`，不得把回條送回已斷線的 actor。

**P8-bb1a899-3：仍無法辨識正式收尾缺口。**

- [custody.rs:1452](crates/sw-core/tests/custody.rs:1452) 的 panic 案例使用已 READY 的一般 YAML session，此時沒有待收尾帳本紀錄。最後檢查 `custody.is_none()`，無法驗證 `ClaimForStop` 接線。
- [custody.rs:1554](crates/sw-core/tests/custody.rs:1554) 的 DR-1 案例自行實作 `LedgerReaperCb`，未經正式 manager 的送達失敗分支；因此上述探針失敗時，它仍通過。
- [custody.rs:1502](crates/sw-core/tests/custody.rs:1502) 的 DR-7 案例暫停已取出的 Request、造成**回覆逾時**，並未製造 Full／入列逾時；也沒有 parked handle、原 token、開埠次數及最後 READY 的斷言。

應以正式 manager 路徑建立上述前置條件，讓正式機制自行收尾，驗證配對終態、實際關閉與送達結果。

本輪驗證：

- `custody`：**36 passed**。
- `session_manager wp510_`：**42 passed**。
- 動態建構失敗新增案例：**2 passed**。
- 獨立故障／生命週期探針：**2 failed**，分別重現 A 與 B 的問題。
- 契約檢查：`ok=true`；Policy：25 passed、0 failed、2 warnings，屬非 PR 情境。
- `git diff --check 9ee1f3e2..7d19d5c8` 通過。

## B 新發現

**P8-7d19d5c-1 [Medium] [session_manager.rs:4241](crates/sw-core/src/session_manager.rs:4241) — 新背景執行緒永久持有 Shared，shutdown／drop 無法釋放。**

兩個執行緒以 `shared.clone()` 作為回呼，停止控制物件又存於 `Shared`；迴圈只檢查自身 `stopped`，而 `SessionManager::drop` 只設定 `shared.closed`。因此無法到達原本負責停止執行緒的 Drop。

具體實測：建立**零 session** 的 manager，完成 shutdown，再 drop 並等待 500 ms。預期依賴釋放、兩個背景執行緒退出；實際 serial 依賴的 Weak 仍可升級，`serialwrap-reap`／`serialwrap-deli` 仍存活。反覆建立與銷毀會持續累積執行緒及整組依賴。

修法：由 `SessionManager` 明確管理兩個執行緒的生命週期；完成必要回收後，在不持有 manager 鎖的情況下停止並 join，解除強引用循環。補上空 manager shutdown／drop 的釋放回歸。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- 出生即接管、bind 終態等待、retired_drains、W6 external attach、W1 actor 化及整包驗收，依 R3～R6 承接。
- 實機 1 MB pull、host 完整 pytest、sw-pal 無 features／Clippy 限制，本輪未重驗。

## 對裁決的異議（不計入結論）

無。

未修改原始碼或工作樹；探針由 stdin 編譯，產物位於指定 `/tmp/solrev-p8-rust-refactor-p8-target`。未跑完整 pytest、Docker／bundle，未操作 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

結論：需修正