# i306 紅綠燈索引

所有紅燈均為預期中的測試／mutation 反例，並非最終候選的失敗。完整 stdout/stderr 原檔分別位於 `red/`、`green/` 和 `acceptance/`；檔案雜湊列在同目錄上層的 `SHA256SUMS`。

| Finding／證明目標 | Red | 綠燈／最終證據 |
|---|---|---|
| V051-d32e47c-3：分段必須逐 snapshot 由 runner 檢查 | `red/red-segment-snapshot-hook-mutation.txt`：停用 test-fakes snapshot 觀察後，`echo_of_complete_probe_payload_never_confirms_ready` 在第一段等待 runner snapshot 時失敗，0 passed／1 failed。這是測試同步哨兵 mutation，不是 production 行為 mutation。 | `green/final-restored-core-self-test-safety.txt`：還原後 25 passed。workspace 最終候選另有 3,680 passed／0 failed。 |
| V051-d32e47c-4：pending 必須是驗收前置 | `red/red-pending-precondition.txt`：原 READY fixture 的 `ready_reconfirm_pending` 為 false，新增前置斷言失敗，0 passed／1 failed。 | `green/green-pending-precondition.txt`：boot banner／`BOOT_DONE` fixture 下 targeted test 通過。最終 RPC node 亦於 workspace 通過。 |
| V051-e77c76a-1：換行前綴自訂 prompt | `red/red-newline-prompt.txt`、`red/red-rpc-newline-prompt.txt`：原實作 actor／RPC 均回 `prompt_after_marker=false`，各 0 passed／1 failed。 | `green/green-core-newline-prompt.txt`、`green/green-rpc-newline-prompt.txt`：actor 同 chunk／分 chunk 及 service RPC regression 均通過。 |
| V051-e77c76a-2：未終止 marker 不得被 `$` 誤認為完整行 | `red/red-old-marker-mutation.txt`：暫時套回舊 `$` 判準後，未終止 marker 加同行 prompt 被錯誤回報 `probe_ok=true`，`execution_marker_requires_terminated_line_before_prompt` 失敗。 | 最終 core focused 25/25 通過；`execution_marker_requires_terminated_line_before_prompt` 先等 runner 檢查 marker snapshot 再注入 prompt，無固定睡眠。 |
| V051-d32e47c-3 的原始 focused red、V051-e77c76a-1 的整檔 red | `red/red-core-self-test-safety.txt`：原實作 24 passed／1 failed；`red/red-daemon-self-test-safety.txt`：原實作 5 passed／1 failed。 | `green/focused-core-self-test-safety.txt`：25 passed；`green/focused-daemon-self-test-safety.txt`：6 passed。 |
| V051-d32e47c-5／V051-e77c76a-3：候選交付證據 | 這是交付完整性項目，沒有可執行的產品紅燈。 | `acceptance/cargo-test-workspace-final.txt`、checks、policy／contract／lock 報告、收集 JSON、此報告與 checksum 對應最終候選 `590e7bbc1447d5e863f7388701c167daa68a2369`。 |

## 測試順序與環境例外

1. 在原始 marker／RPC 實作上先取得 newline prompt red；pending 前置斷言也先在原 READY fixture 上轉紅。
2. 暫時恢復舊 marker `$` 判準，取得行界 mutation red；恢復新判準後才記 green。
3. 為確認分段同步真的依賴 runner checkpoint，暫停 test-fakes 的 snapshot-end 寫入，取得 `red-segment-snapshot-hook-mutation.txt`；mutation 還原後重新跑完整 core self-test safety，25 passed。
4. daemon crate 首輪完整測試缺少 `SW_TEST_SERIALWRAP_NO_FAKES`，在 helper 的前置檢查失敗。獨立建置 no-fakes CLI 並設定該變數後，`acceptance/cargo-test-sw-daemon-configured.txt` 全綠；未使用主機 pipx CLI 或 daemon。

收集／鎖檔：完整 cargo collect 為 3,729 nodeids；交給 add-only 工具的 scoped collect 保留既有 3,674 nodes 並加上本項兩個新 nodes。三個 Linux 集合各寫入 2 筆、0 移除；53 個無關新增節點在 `collect/unregistered-out-of-scope-nodeids.txt` 明列並排除。
