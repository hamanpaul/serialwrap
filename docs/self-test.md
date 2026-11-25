# Self-test 行為與回應

`session self-test` 是查詢某一刻 session 狀態的診斷操作。它不會鎖定目標，也不會替下一個 RPC 保留操作權；self-test 返回後，命令、其他 RPC 與 console 操作仍依一般仲裁規則進行。

```bash
serialwrap session self-test --selector COM0
```

## 忙碌時：未探測

Rust self-test 在下列情況被動回覆，不送 UART probe、不 suspend human、不更新 `last_probe_at`，也不確認或清除 readiness pending：

- 有前景命令、有效待執行命令或 file transfer。
- login、Recover、另一個 self-test probe 或 agent interactive lease 正在進行。
- 有尚未結束的 background capture。

一般忙碌回 `classification: "SESSION_BUSY"`；有效 agent lease 回 `classification: "SESSION_INTERACTIVE_BUSY"`。兩者都帶 `recommended_action: "wait"`，不帶 `probe_ok` 或 `probe_evidence`。**忙碌表示沒有測試，不表示故障**，不應只因 busy 執行 Recover。

等待前景操作或 background capture 自然結束後可重新診斷。依既有規則，提交下一個命令也會結束 background capture；完成後再呼叫 self-test。

明示 Recover 仍可受理。若 Recover 介入正在進行的 self-test，舊 probe 的證據會失效；晚到結果不能確認新狀態或清除新 pending。正常成功或失敗會配對結算本次 human suspend，並依原規則回放 deferred human bytes；Shutdown Phase 1 立即回覆 `FLUSHED_BY_SHUTDOWN`，晚到結果不再回覆或回放。

## 執行 probe 時：驗證命令確實執行

只有沒有命中忙碌或其他既有前置分類時，Rust 才以一次 actor 仲裁取得 probe 的 foreground slot、pending 身分與 Fence 授權。probe 結束前其他前景命令不能插入；提交時 Fence 會再核對目前的 probe op owner。

Rust 將 nonce 放入設定的 `ready_probe`，再包在 POSIX shell 執行框架內：

```sh
( <ready_probe，已替換 nonce> ) && printf '\n__SW_ST_%s_OK__\n' '<nonce>'
```

完整預期標記不在 UART TX 中。`&&` 使標記只有在設定的 probe 命令回傳零時才會輸出。成功還要求標記是包含正確 nonce 的完整獨立輸出行，且 `prompt_regex` 必須在標記結束位置之後成立。只回顯 probe、只出現標記前 prompt、probe 回傳非零、標記錯誤／被輸出插斷、或標記後沒有 prompt，都不能確認 READY。

已執行 probe 的 Rust 回應包含唯讀物件 `probe_evidence`：

| 欄位 | 意義 |
| --- | --- |
| `tx_complete` | 完整 probe 框架 payload 已送出 |
| `execution_marker` | 收到 nonce 對應的完整獨立執行標記 |
| `probe_rc_zero` | 設定的 probe 命令成功，因而執行 `&&` 後的標記 |
| `prompt_after_marker` | 標記結束位置之後符合 prompt |

只有四項皆為 `true`，且 op 身分、連線 epoch、FSM／boot 證據世代與 Fence 授權仍有效，才會回 `probe_ok: true` 並呼叫 `confirm_ready()`。執行過 probe 的失敗回應也會保留四項實際證據；早退或 busy 回應不宣稱執行過 probe。

## 相容性與範圍

此修正是 Rust-only 的 BC-13。Python 0.4.x 程式與 golden 維持凍結；RPC schema 保留 Python 分支，另外標示 Rust busy 與 execution evidence 語意。Rust 框架會改變實際 TX payload 與某些拒絕回應的長度，因此 Rust 測試依實際 payload 驗證，Python golden 不重錄。

`ready_probe` 必須能在相容的 POSIX shell 框架中執行。非相容的自訂 probe 會驗證失敗，不退回 nonce 回顯判準。Passthrough 仍依既有規則不送此 probe。

issue 306／307 的回歸由受控 fake DUT、CoreHarness 與 ServiceRig RPC 測試覆蓋，包含忙碌零 TX、probe／命令仲裁順序、回顯假成功、執行失敗、標記後 prompt、世代失效、Shutdown 與 human bytes 回放；本缺陷不另加真實硬體 regression case。Windows 原生測試與真實 DUT 自訂 shell 相容性尚未驗證，維持 BLOCKED-OWNER。
