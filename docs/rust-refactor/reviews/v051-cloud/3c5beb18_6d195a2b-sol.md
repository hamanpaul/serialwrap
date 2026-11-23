> 審查者：codex gpt-6.1-terra xhigh（擁有者 2026-11-23 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| V051-9130685-1 | High | 已修 `278ed340` | 2（已結案） | 修正仍保留；未重跑原回歸。 |
| V051-fc8a441-1 | High | 已修 `278ed340` | 0 | 修正仍保留；未重跑原回歸。 |
| V051-8ce904e-1 | High | 已修 `6f559254` | 0 | 重送安全契約仍保留；未重跑原回歸。 |
| V051-6f55925-1 | High | 已修 `a0313bd5` | 0 | BC 集合與核准來源檢查仍保留；未重跑原回歸。 |
| V051-3c5beb1-1 | High | 未修 | 1 | `6d195a2b` 已將 `PendingOp::Recover` 加入忙碌判定，但未補前輪要求的 READY Recover／self-test 受控交錯、零 probe TX 測試。另有新判定未排除失效操作的問題，見 B。 |
| V051-3c5beb1-2 | High | 已修 `6d195a2b` | 0 | 有效 agent lease 已回 `SESSION_INTERACTIVE_BUSY`，對應測試通過。兩份出貨 SKILL 仍寫 `SESSION_BUSY`，依本次範圍說明留待第 6 項文件接線。 |
| V051-3c5beb1-3 | High | 未修 | 1 | 鎖定節點已改為預期 `SESSION_BUSY`，定向測試通過；但所附裁決未授權修改此節點，也未增加零 probe TX 斷言。須先由編排者裁定，再依裁決處理。 |
| V051-3c5beb1-4 | Medium | 未修 | 1 | 背景測試仍只等待 `running`，未確認前景槽空閒且 capture 仍為 `active`；前景忙碌即可使斷言通過。 |

## B 新發現

- **V051-6d195a2-1 [Medium]** [session_core.rs:8883](crates/sw-core/src/session_core.rs:8883) — 新判定把所有尚未回收的 `PendingOp::Recover` 都視為有效，沒有檢查其 `side_ops` 登錄及世代。依程式路徑推導：READY Recover 送出救援鍵後仍在等 prompt，此時發生 Rebound，drain 完成會清除 `side_ops`、解除 recovering，卻保留 pending waiter 等待晚到的 `OpFinished`；在等待期間呼叫 self-test，實際回 `SESSION_BUSY`，而 bridge 已失效，本應回 `BRIDGE_DOWN`。這會暫時掩蓋裝置狀態。**修法**：只把仍屬目前有效世代、仍登錄中的 Recover 視為忙碌；補受控交錯測試，分別驗證有效 Recover 期間零 probe TX，以及世代失效後不再因舊 waiter 回 busy。

## C 完整性核對

第 1 項的前景、有效 queue、login、agent lease、background capture 忙碌判定已有實作；READY Recover 判定有 B 所述問題，且缺受控回歸測試。背景 capture 測試未證明其獨立判定生效。第 2～6 項依指定範圍留待後續完成。

定向測試結果：

- `self_test_safety`：**2 passed、1 failed**。失敗是既有前景測試的 TX 次數斷言，實際 3、預期 2；單獨重跑該節點通過。測試只等待命令進入 `running`，沒有先確認命令 TX 完成，因此此結果尚不能證明多出的 TX 是 probe。
- `self_test_while_foreground_command_sends_probe`：**1 passed**；其鎖定斷言的授權問題仍如 A 所述。

未執行完整 pytest、Docker 或 bundle 測試；工作樹保持乾淨。

## 既有碼待辦（不計入結論）

[前景安全測試](crates/sw-core/tests/self_test_safety.rs:33) 的 TX 取樣應先同步到前景命令 TX 完成，才能可靠判斷 self-test 是否新增 TX。此測試不屬本輪 diff。

## 對裁決的異議（不計入結論）

無。

結論：需修正