> 審查者：codex gpt-6-astra（擁有者 2026-11-08 指定）

## A 前輪 finding 核對

HEAD：`e385ed0a`，工作樹乾淨。本輪範圍只有一筆提交，已逐一檢查全部差異，無 merge。未找到 `p8-fix-responses.md`；已核對本輪修正報告與相關裁決。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| P8-7bb9935-1 | High | 已修 `4fca6390` | 0 | 認領與單次在途回歸通過。 |
| P8-7bb9935-2 | Medium | 已修 `4fca6390` | 0 | 原退休 drain 收斂回歸通過。 |
| P8-7bb9935-3 | Medium | 已修 `4fca6390` | 0 | bind／clear 首次期限回歸通過。 |
| P8-7bb9935-4 | Medium | 已修 `424aee24` | 0 | 保管路徑 clear／拔除回歸通過。 |
| P8-7bb9935-5 | Low | 已修 `354a96e2` | 0 | 出生即接管修正保留。 |
| P8-057a065-4 | Medium | 依裁決不處理 | 2 | s2 裁決保留；真實 Held handle 的 panic 收尾回歸通過。 |
| P8-591d2ff-1 | Low | 未修 | 47 | DF-27 固定要求 APPROVED 的斷言仍在。 |
| P8-0896933-1 | Low | 未修 | 39 | DF-25 報告統計及時間誤植仍在。 |
| P8-c89cd78-1 | Low | 未修 | 37 | DF-26 固定要求 APPROVED 的斷言仍在。 |
| P8-6a679fb-4 | Low | 已修 `354a96e2` | 0 | 回收者停止後 fallback 保留。 |
| P8-03333f7-2 | Low | 已修 `354a96e2` | 0 | parked 認領條件保留。 |
| P8-03333f7-3 | Low | 已修 `354a96e2` | 0 | delivered 計數邊界未改。 |
| P8-7e34afb-1 | Low | 已修 `354a96e2` | 0 | PROBE 鉤子鎖外呼叫保留。 |
| P8-566d90b-5 | Low | 已修 `354a96e2` | 0 | shutdown／退休 bridge 回歸通過。 |
| P8-c92ef3e-1 | Low | 已修 `354a96e2` | 0 | recover 與過期通知回歸通過。 |
| P8-a07fdfb-1 | High | 已修 `02bd822c` | 0 | 不可撤銷的裝置取消回歸通過。 |
| P8-add3ca0-1 | High | 已修 `c3f78960` | 0 | 原 parked close 遲到收尾回歸通過。 |
| P8-2c5eb6b-1 | High | 已修 `c3f78960` | 0 | 跨凍結遲到完成回歸通過。 |
| P8-2c5eb6b-2 | High | 已修 `c3f78960` | 0 | 改綁後拒絕舊完成回歸通過。 |
| P8-2c5eb6b-3 | Medium | 已修 `c3f78960` | 0 | 首次期限共用回歸通過。 |
| P8-6ca9bc8-1 | Medium | 已修 `c3f78960` | 0 | 原路徑欄位回歸通過；新增交錯另見 B。 |
| P8-0c50830-1 | High | 已修 `c3f78960` | 0 | release 無前置快照查詢的路徑保留。 |
| P8-990a04b-1 | High | 已修 `c3f78960` | 0 | 正式 RPC 交錯回歸通過。 |
| P8-398b85f-1 | High | 已修 `e35edc09` | 0 | W6 歷史 ActorId 隔離回歸通過。 |
| P8-21482ec-1 | High | 已修 `e35edc09` | 0 | W6 owner／回收身分回歸通過。 |
| P8-e35edc0-1 | High | 已修 `5e357c76` | 0 | 指定 actor 的身分斷言保留。 |
| P8-6536d16-1 | High | 已修 `9ab75ab0` | 0 | shutdown 共用期限與阻塞後收尾回歸通過。 |
| P8-9ab75ab-1 | Medium | 已修 `f661291b` | 0 | 逾時不空轉及遲到回條回歸通過。 |
| P8-849e605-1 | High | 已修 `0b37df5d` | 0 | Reaper 關閉屏障及成功／遲到／失敗回歸通過。 |

前輪未逐列提供的 37 項已修 finding，以及 P8-566d90b-1、P8-d7af8df-1／-2，沿用前輪結案證據，不補造編號。

## B 新發現

**P8-e385ed0-1 [High]** [crates/sw-core/src/session_core.rs:3647](crates/sw-core/src/session_core.rs:3647) — **新增的 Bridged bind／release 交錯遺失在途作業的收尾語意，關閉成功後仍停在 RECOVERING。**

具體重現，全部經正式 `Service::rpc`：

1. COM0 已 RELEASED，呼叫 `device.attach`，暫停在帳本已持有 Bridged worker、尚未送出 `BridgeOpened`。
2. 呼叫 `session.bind` 改綁離線目標，確認同 token 已寫入 Bind 取消；再呼叫 `device.release`。
3. 等待 bind／release 的期限到，分別取得 `SESSION_RECOVERING`／`DRAIN_STALLED`。
4. 放行 `BridgeOpened`，等待真實 handle 關閉、帳本清空。

預期依 Python `_finish_attach_op`（`origin/main:sw_core/session_manager.py:4339–4363`）及既有裁決：遲到收尾恢復 `RELEASED`、清除兩個 drain 欄位並落盤。實際穩定重現：

```text
state = RECOVERING
last_error = RELEASED
drain_stalled = false
drain_timeout_epoch = 1
```

原因是本輪移除 custody token 等待後，遇到 `pending_open_stop` 便走第 3671 行的 `bridge_opened_inner`，進而轉為一般 `StopOp`。後續 `worker_stopped` 的 `already_released` 分支不恢復 RELEASED，也未執行 [finish_pending_open_stop:3929](crates/sw-core/src/session_core.rs:3929) 的清欄位及落盤流程。移除等待後，`retired_drains_pending` 鏡像亦殘留為 true。

**確切修法：**轉移 token 至 epoch 等待時保留 `PendingOpenStop` 的在途 release 語意；以配對退休排空回條收斂，再由 `finish_pending_open_stop` 完成狀態、欄位及持久化，並同步更新退休等待鏡像。補入上述「bind＋release 均逾時後才完成 Bridged 交接」的正式 RPC 回歸，保留期限內完成的對照案例。

獨立驗證：以 stdin 編譯測試，未寫入來源檔。上述 RPC 案例連續兩次失敗；期限內完成的對照通過。失敗測試執行檔：[astra-i21-rpc-probe](/tmp/solrev-p8-rust-refactor-p8-target/astra-i21-rpc-probe)。

| 本輪定向驗證 | 結果 |
|---|---:|
| `sw-core custody wp510r_` | 12 passed |
| `sw-core drain_release wp510r_` | 71 passed |
| `sw-core session_manager wp510r_` | 30 passed |
| `sw-daemon dispatch wp510r_` | 34 passed |
| `sw-daemon shutdown_order` | 11 passed |
| L1 結構比對 | 僅新增六個節點，原欄位、selector、順序保留 |
| 契約檢查 | `ok=true`，0 findings |
| Policy check | 25 PASS／0 FAIL／2 WARN；非 PR 上下文 |
| 增量 `git diff --check` | 通過 |

新增六支回歸均通過，但未涵蓋上述停止交錯，因此本包收尾驗收尚未完整。未跑完整 pytest、Docker／bundle；未接觸 production daemon、指定設定／狀態目錄或實體 UART。編譯產物置於指定 `/tmp` target，驗證後工作樹仍乾淨。

## 既有碼待辦（不計入結論）

沿用前輪，未另行結案：

- 完整 shutdown 與 parked close 遲到回條的停止順序。
- 無 release 介入時，parked 關閉期間重插被舊拔除事件覆蓋的情境。
- 保管開埠途中 clear 的 DETACHED／CLEARED 差異。

## 對裁決的異議（不計入結論）

無。

結論：需修正