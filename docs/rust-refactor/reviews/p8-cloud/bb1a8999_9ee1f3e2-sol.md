> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`9ee1f3e254b58b02cd55f05ce0579fe424fb7607`，detached，工作樹乾淨。本輪僅 `9ee1f3e2`，五個變更檔均已檢查，無 merge。未找到 `p8-fix-responses.md`。下列既有修正提交皆為 HEAD 祖先；未受本輪影響者沿用前輪證據。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-3786805-1 | High | 已修 f61de224 | 0 | 原始 selector 傳遞保留。 |
| P8-19df186-1 | High | 已修 f40c01f0 | 0 | 佔位退休與 release 取消仍共用鎖；定向回歸通過。 |
| P8-19df186-2 | High | 已修 f40c01f0 | 0 | timeout／settled token 配對保留。 |
| P8-f40c01f-1 | High | 已修 693a29ea | 0 | 未確認關閉仍阻擋；持久化重試案例通過。 |
| P8-693a29e-1 | High | 已修 abeff2da | 0 | 未取得 handle 的開埠失敗收尾案例通過。 |
| P8-fdc1413-1 | High | 已修 8196bca6 | 0 | 鎖內重讀 `close_failed` 的回歸通過。 |
| P8-503c341-1 | High | 已修 6334a896 | 0 | bind 認領並關閉 parked 作業的既有路徑保留。 |
| P8-e25b69c-1 | High | 已修 8463b22c | 0 | manager 雙 release 共用首次期限，定向案例通過。 |
| P8-e25b69c-2 | High | 已修 b1a2e83a | 0 | fallback 遲到完成案例通過。 |
| P8-8463b22-1 | High | 已修 14fe5922 | 0 | 撤銷標記保留至開埠終態。 |
| P8-14fe592-1 | High | 已修 c4913aed | 0 | retired bridge 回條等待保留；時點調整仍屬 R3。 |
| P8-c4913ae-1 | High | 依裁決不處理 | 3 | 由 R3 承接，維持輪數。 |
| P8-1533cb5-1 | High | 已修 2a660d96 | 0 | atomic 鏡像與鎖內 TX fence 保留；零 TX 回歸通過。 |
| P8-a190a57-1 | Medium | 已修 a81ff33b | 0 | artifact 選取與環境還原未改。 |
| P8-a190a57-2 | Medium | 已修 479f8d2b | 0 | TX 串接與交錯斷言未改。 |
| P8-a81ff33-1 | Medium | 已修 479f8d2b | 0 | 假 repo／artifact 與建置目錄隔離未改。 |
| P8-591d2ff-1 | Low | 未修 | 18 | `tests/test_contracts_catalog.py:1451` 仍固定要求 DF-27 為 `APPROVED`。 |
| P8-a94151a-1 | Medium | 已修 3c357879 | 0 | 明確 release／Drop 放行及清除 paused 未改。 |
| P8-32219f2-1 | Medium | 已修 08969338 | 0 | 可追溯報告與失敗／待驗收聲明保留。 |
| P8-0896933-1 | Low | 未修 | 10 | 驗收報告統計與時間欄位本輪未修。 |
| P8-c89cd78-1 | Low | 未修 | 8 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-26 為 `APPROVED`。 |
| P8-057a065-1 | High | 已修 8f2d661b | 0 | 帳本單元層資源認領案例通過；正式接線仍見 P8-bb1a899-1。 |
| P8-057a065-2 | High | 已修 50e1e798 | 0 | stop_id 配對與錯配阻擋案例通過。 |
| P8-057a065-3 | Medium | 已修 50e1e798 | 0 | 凍結時限案例通過。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | 已核對 `origin/docs/parity-amendments` 的 `rulings/s2.md`；不要求 panic 超限退出。 |
| P8-8f2d661-1 | High | 已修 50e1e798 | 0 | 缺通道歸還所有權及補通道結算案例通過。 |
| P8-50e1e79-1 | Medium | 已修 03eb4d32 | 0 | 解凍後延遲輪詢案例通過。 |
| P8-03eb4d3-1 | Medium | 已修 6ebd0d2e | 0 | 實際凍結區間計時案例通過。 |
| P8-6ebd0d2-1 | Medium | 已修 67a1bc10 | 0 | 歷史區間截斷至取樣時刻的案例通過。 |
| P8-bb1a899-1 | High | 未修 | 1 | 正常交接快照已修，但真實資源遷移、回收者及故障收尾仍未完成，詳下。 |
| P8-bb1a899-2 | High | 已修 9ee1f3e2 | 0 | 分開 `close_failed` 與 `stalled`；雙 release 首次期限回歸通過。 |
| P8-bb1a899-3 | Medium | 未修 | 1 | panic 案例仍自行結算獨立帳本；新增 manager 建構失敗案例沒有預開 handle，詳下。 |

**P8-bb1a899-1：部分修正，尚不能結案。**

`register` 拒絕已處理，正常交接／release 案例亦通過。但 [session_manager.rs:2456](crates/sw-core/src/session_manager.rs:2456) 仍把真實資源存入 `op.preopened`，帳本只寫入 `Held { parts: None }`。`adopt_bridged`、`claim_bridged_worker` 仍無正式呼叫者；新接上的退出通知，其接收端仍在 [session_manager.rs:3968](crates/sw-core/src/session_manager.rs:3968) 建構後被丟棄。

獨立探針經正式 manager 路徑重播：

1. 建立 `gone-template:COM3` 佔位與假裝置。
2. 注入下一次 actor 建構失敗。
3. 執行佔位 attach，等待 attach 作業收尾。

實際結果：

```text
attach.ok = true
sessions = []
close_count = 0
custody = Held
owner = Session { sid: "gone-template:COM3", inc: 0 }
```

預期已開 handle 必須明確關閉並取得回條，成功後結算帳本。實際 [session_manager.rs:1544](crates/sw-core/src/session_manager.rs:1544) 以新 sid 清理，帳本仍屬舊佔位，資源也不在帳本內；探針的關閉次數斷言失敗。假後端 Drop 釋放占用不等於取得關閉回條。

修正仍須完成真實 handle／worker 的帳本移交、認領及終態結算，接通回收命令消費者，並以正確 token／owner 處理建構失敗；不可僅刪紀錄或改快照。此為原 finding 的 R2 接線缺口，不要求提前完成 R3～R6。

**P8-bb1a899-3：新增測試仍不能辨識上述缺口。**

[custody.rs:1463](crates/sw-core/tests/custody.rs:1463) 雖建立真實 actor，卻使用 `ReplyExec`；帳本是測試區域變數，owner 為 `COM40`，actor 則來自 `SHELL_PROFILE` 的 `t:COM0`。最後仍由測試在第 1472 行自行呼叫 `claim_for_stop`，移除正式停止接線也不會讓此測試變紅。

新增的 [custody.rs:1798](crates/sw-core/tests/custody.rs:1798) 只覆蓋 YAML 初始化建構失敗，沒有先持有 handle／帳本紀錄。它通過，但上述動態交接探針失敗。DR-7 的實際 Full／重送接線驗證亦未補齊。

應經正式 manager／actor 注入故障，讓停止機制自行收尾，斷言實際關閉次數、配對回條、帳本終態及送達結果。

本輪驗證：

- `custody`：**34 passed**。
- `session_manager wp510_`：**42 passed**。
- 新增正常交接／release 案例：**1 passed**。
- 獨立動態建構失敗探針：**1 failed**，重現上述未修缺口。
- 契約檢查：`ok=true`；Policy：25 passed、0 failed、2 warnings（非 PR 情境）。
- `git diff --check bb1a8999..9ee1f3e2` 通過。
- 未修改原始碼；探針從 stdin 編譯，建置產物位於指定 `/tmp/solrev-p8-rust-refactor-p8-target`。未跑完整 pytest、Docker／bundle，未操作 production daemon、指定設定／狀態目錄或實體 UART。結束時 HEAD 未變、工作樹乾淨。

## B 新發現

NO FINDINGS。

本輪確認的阻擋項均屬 A 中尚未修完的既有 finding，不另編新號。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- 出生即接管、bind 終態等待、retired_drains、W6 external attach、W1 actor 化及整包驗收，依 R3～R6 承接。
- 實機 1 MB pull、host 完整 pytest、sw-pal 無 features／Clippy 限制，本輪未重驗。

## 對裁決的異議（不計入結論）

無。

結論：需修正