> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`cdae01df`。已逐一審查 `b7ac1d04`、`cdae01df`，無 merge。下列修正提交皆為 HEAD 祖先；本輪未改動其修正邏輯或削弱既有回歸斷言。

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
| P8-591d2ff-1 | Low | 未修 | 2 | `tests/test_contracts_catalog.py:1439` 仍固定要求 DF-27 為 `APPROVED`；維持 low-backlog。 |

## B 新發現

NO FINDINGS。

核對結果：

- `b7ac1d04` 在正式 `run_reboot` 路徑、送出命令前清除 RX；非空 `console_unlock_env` 條件與 Python 一致。清除保留累計 offset、RX 統計及 WAL。
- 兩個新增測試分別涵蓋 actor 與正式 `Service::rpc(command.submit)` 路徑，驗證殘留 prompt 不會省略 unlock secret，並恢復 READY。
- `cdae01df` 僅在兩個既有 L1 集合各追加兩個節點；沒有移除節點、改動 selector 或放寬判準。

定向驗證：

- `command_exec` 的 `reboot` 集合：**17 passed**，包含新增核心測試。
- `dispatch` 的 `wp73_`：**1 passed**。
- `tools.contracts check`、`regen_lock.py --check --base b5bdca56`：通過。
- `git diff --check`：未通過，僅報 `command_exec.rs:4648`、`dispatch.rs:8699` 檔尾多餘空白行，屬不擋放行的格式問題。

未執行完整 pytest、docker／bundle 或 production daemon 測試。建置產物使用指定 `/tmp` 目錄；未編輯檔案，結束時工作樹乾淨。

## 既有碼待辦（不計入結論）

- 沿用前輪：正式 `human_active` 接線、一般 RELEASED session 開埠失敗再遇落盤失敗的終態差異。
- P8-c4913ae-1 由 wp510r 承接；df25、od79、od80 保留各自工作包範圍。

## 對裁決的異議（不計入結論）

無。

結論：可放行