> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-9130685-1 | High | 已修 `278ed340` | 2（已結案） | 修正仍保留；本輪未重跑原回歸。 |
| V051-fc8a441-1 | High | 已修 `278ed340` | 0 | 修正仍保留；本輪未重跑原回歸。 |
| V051-8ce904e-1 | High | 已修 `6f559254` | 0 | 重送安全契約仍保留。 |
| V051-6f55925-1 | High | 已修 `a0313bd5` | 0 | BC 集合與核准來源檢查仍保留。 |
| V051-3c5beb1-1 | High | 已修 `4d0e06c7` | 1（已結案） | READY Recover 等待期間的零 TX 測試仍保留。 |
| V051-3c5beb1-2 | High | 已修 `6d195a2b` | 0 | 有效 agent lease 分類與文件仍保留。 |
| V051-3c5beb1-3 | High | 未修 | 3 | [鎖定節點](crates/sw-core/tests/command_exec.rs:3438) 的預期已變更，仍未找到具名授權；實作者已列入 SPEC-GAPS，等待裁決。 |
| V051-3c5beb1-4 | Medium | 已修 `4d0e06c7` | 1（已結案） | 測試確認前景空閒、capture active，並檢查結束後不再擋 probe。 |
| V051-6d195a2-1 | Medium | 已修 `df90a088` | 0 | Rebound 後撤銷的舊 waiter 不再遮蔽 `BRIDGE_DOWN`。 |
| V051-df90a08-1 | High | 已修 `5f56eb9e` | 0 | human attach 推進 owner 世代的指定情境已有零 TX 測試；FSM 世代的另一個交錯見 B。 |
| V051-df90a08-2 | High | 已修 `5f56eb9e` | 0 | 前置分類先於 busy 判定；passthrough、quiet、bridge 異常有對應測試。 |
| V051-df90a08-3 | Medium | 已修 `5f56eb9e` | 0 | 新測試隔離有效 queue 與前景槽；實作者日誌另有移除 queue 判定後轉紅的證據。 |

## C 完整性核對

以下依本次指定的第 1／6 項判定；明確排入後續項的工作不當作本項缺漏。

| 項目 | 狀態 | 證據 |
|---|---|---|
| 1. 忙碌零 TX、保留前置分類 | 部分完成 | `3c5beb18`、`6d195a2b`、`5f56eb9e` 實作判定與分類；Recover 的 FSM 世代交錯仍有 B 所述缺口，未列入實作者 SPEC-GAPS。 |
| 2. 同次仲裁保留前景槽 | 未做 | 依指定拆分留待第 2 項。 |
| 3. 執行標記、零 RC、標記後 prompt | 未做 | 依指定拆分留待第 3 項。 |
| 4. 世代失效與 Shutdown 完整收尾 | 部分完成 | 舊 Recover waiter 已處理；完整收尾依拆分留待第 4 項。 |
| 5. 先紅後綠及 core、Fence、RPC 測試 | 部分完成 | 本項已有 core 測試與實作者記錄的紅綠證據；B 的交錯尚無測試。Fence、RPC 測試留待後續項。 |
| 6. BC-13、coverage、RPC schema | 未做 | 依指定拆分留待第 5 項。 |
| 7. 文件與交付 | 部分完成 | 已有 fragment、兩份 SKILL 的 busy 說明；其餘文件與整體交付留待第 6 項。 |

本輪自行執行隔離的 `self_test_safety` 定向測試：**13 passed、0 failed**。未跑完整 pytest、Docker、bundle、Windows 原生或實機測試；工作樹保持乾淨。

## B 新發現

- **V051-5f56eb9-1 [High]** [session_core.rs:8923](crates/sw-core/src/session_core.rs:8923) — 本輪修正移除了 Recover 的 owner 世代限制，但仍以 FSM 世代相等判斷 Recover 是否忙碌；該條件不能代表救援鍵已失效。具體情境：READY Recover 已送 Ctrl-C、正在等 prompt；接著收到 `U-Boot`，quiet arm 推進 FSM 世代。兩秒等待結束前，若收到出貨 prpl profile 的 `boot_done_regex` 文字 `Please press Enter to activate this console`，quiet 會解除，但此文字不匹配 Recover 的 shell prompt。此時 Recover 的 side-op 仍在、仍可能送 Ctrl-D；self-test 卻因 FSM 世代不同而略過 busy，建立 probe，兩者可交錯。

  這是依 [quiet 狀態轉移](crates/sw-core/src/quiet_gate.rs:176)、[Recover 等待與救援鍵路徑](crates/sw-core/src/op_runner.rs:905)及 [Fence 判定](crates/sw-core/src/tx_writer.rs:523) 的靜態推導，本輪未實跑此交錯。第 1 項要求 Recover 進行中零 probe TX；實作者回報的 SPEC-GAPS 未包含此缺口。

  **修法：**以 Recover side-op 是否仍登錄、連線是否有效及其實際提交資格判定忙碌，不把單純 FSM 世代變化當成撤銷；保留 Rebound 舊 waiter 不遮蔽 `BRIDGE_DOWN` 的行為。補「Ctrl-C → banner → 非 prompt 的 boot-done 訊號 → self-test → Recover 後續鍵」受控交錯，斷言 self-test 被動回覆且零 probe TX。

## 既有碼待辦（不計入結論）

後續第 2～6 項仍需完成 probe 前景槽、執行證據、完整收尾、BC-13／RPC 契約及整體接線驗收。

## 對裁決的異議（不計入結論）

無。

結論：需修正