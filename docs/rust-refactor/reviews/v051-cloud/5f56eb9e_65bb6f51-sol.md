> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-9130685-1 | High | 已修 `278ed340` | 2（已結案） | 修正保留。 |
| V051-fc8a441-1 | High | 已修 `278ed340` | 0 | 修正保留。 |
| V051-8ce904e-1 | High | 已修 `6f559254` | 0 | 修正保留。 |
| V051-6f55925-1 | High | 已修 `a0313bd5` | 0 | 修正保留。 |
| V051-3c5beb1-1 | High | 已修 `4d0e06c7` | 1（已結案） | 原回歸測試保留。 |
| V051-3c5beb1-2 | High | 已修 `6d195a2b` | 0 | 修正保留。 |
| V051-3c5beb1-3 | High | 依裁決不處理 | 3（已結案） | 編排者已明確核准 F25 節點改期望；`65bb6f51` 補上指定註解。 |
| V051-3c5beb1-4 | Medium | 已修 `4d0e06c7` | 1（已結案） | 原回歸測試保留。 |
| V051-6d195a2-1 | Medium | 已修 `df90a088` | 0 | Rebound 的 `BRIDGE_DOWN` 定向測試通過。 |
| V051-df90a08-1 | High | 已修 `5f56eb9e` | 0 | human attach 交錯的定向測試通過。 |
| V051-df90a08-2 | High | 已修 `5f56eb9e` | 0 | 前置分類定向測試通過。 |
| V051-df90a08-3 | Medium | 已修 `5f56eb9e` | 0 | queue 定向測試通過。 |
| V051-5f56eb9-1 | High | 已修 `65bb6f51` | 0 | 指定的 Ctrl-C → banner → boot-done → self-test → Ctrl-D 交錯測試通過；本輪新增的其他交錯見 B。 |

## B 新發現

- **V051-65bb6f5-1 [High]** [session_core.rs:8964](crates/sw-core/src/session_core.rs:8964) — Recover 忙碌判定把「此刻 Fence 暫時拒絕提交」當成 Recover 已無提交資格。具體情境：READY Recover 已送 Ctrl-C、仍在等待 prompt；此時 WAL 寫入暫時失敗，`wal_unhealthy=true`。self-test 判定時 `Fence::admit` 回 `WalUnhealthy`，因而略過仍登錄的 Recover 並建立 probe；在 probe 實際提交前，另一次 WAL append 成功使健康狀態恢復。接著 probe 可送出，而 Recover 等待逾時後仍可送 Ctrl-D，違反 Recover 期間 probe 零 TX。這是根據 [Fence 判定](crates/sw-core/src/tx_writer.rs:523)、[WAL 健康恢復](crates/sw-core/src/wal.rs:605)與 Recover 路徑的靜態推導，尚未實跑該交錯。

  **修法：**區分永久撤銷／失效與 WAL、暫時凍結等可恢復的提交限制；Recover 仍登錄且可能恢復提交時維持忙碌。補受控測試，在判定後恢復 WAL，確認 self-test 被動回覆且零 probe TX，Recover 後續鍵仍依既有行為執行。

- **V051-65bb6f5-2 [High]** [session_core.rs:8964](crates/sw-core/src/session_core.rs:8964) — 新增的同步 `Fence::with` 可能讓 self-test 卡住整個 session actor。具體情境：READY Recover 的救援鍵進入 OS 提交，該提交持有 Fence 鎖且暫停；此時 self-test 到達，actor 等待同一把鎖，無法回覆預期的 `SESSION_BUSY`，後續 `GetState`、release 或 shutdown 控制事件也無法由該 actor 處理，直到 OS 提交返回。[FenceSubmitGate](crates/sw-core/src/tx_writer.rs:600) 在持鎖時呼叫 OS 提交；既有測試也明確要求 actor 查詢不得等待這把鎖。本項為靜態推導，尚未實跑新增交錯。

  **修法：**讓 actor 的忙碌判定不等待 TX Fence；例如維護可安全讀取的有效性狀態，或在無法立即取鎖時保守判忙。補 `PauseInsideSubmit` 受控測試，確認 self-test 與後續查詢均能及時回覆，且不新增 probe TX。

## C 完整性核對

依前輪報告指定的拆分，本輪核對第 1 項及其前輪 finding；第 2～6 項仍依拆分留待後續工作。本輪未檢視完整 brief，無法獨立確認其餘交付狀態。

定向測試結果：`self_test_safety` **13 passed**；boot-done Recover 交錯與 F25 前景忙碌節點各 **1 passed**。未跑完整 pytest、Docker、bundle、Windows 原生或實機測試。工作樹保持乾淨。

## 既有碼待辦（不計入結論）

後續仍需完成前景槽、probe 執行證據、完整收尾、BC-13／RPC 契約及整體接線驗收。

## 對裁決的異議（不計入結論）

無。

結論：需修正