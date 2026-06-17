## 1. #51 command-capable 判準與錯誤碼

- [ ] 1.1 RED：寫 fake-PTY 測試——空 `ready_probe` 的 passthrough session attach 後維持 `ATTACHED`；`cmd submit --mode line` 回 `PROFILE_NOT_COMMAND_CAPABLE`（非 `SESSION_NOT_READY`）。先看它失敗。
- [ ] 1.2 RED：寫測試——有 `ready_probe`（+ 可匹配 prompt）的 passthrough target attach 後進 `READY`、`cmd submit` 被接受。
- [ ] 1.3 GREEN：`session_manager._attach_by_id` 與 `_probe_existing_bridge` 以 `command_capable = bool(profile.ready_probe.strip())` 取代 `platform == "passthrough"` 寫死 `ok=False`。
- [ ] 1.4 GREEN：cmd submit gate 對 `ATTACHED` 且非 command-capable 回 `PROFILE_NOT_COMMAND_CAPABLE`（附 hint）；保留 `SESSION_NOT_READY` 給 command-capable 但未 READY。
- [ ] 1.5 確認 1.1–1.2 測試轉綠。

## 2. #51 self-test command_capable 欄位

- [ ] 2.1 RED：測試 `self_test` 所有 classification 的 result 都含 `command_capable`（空 ready_probe→False、非空→True）。
- [ ] 2.2 GREEN：`self_test` 輸出加 `command_capable`。
- [ ] 2.3 確認測試轉綠。

## 3. #51 U-Boot command profile + 真機驗證

- [ ] 3.1 新增 `uboot-template`（`profiles/default.yaml` + `config.py` 預設）：`prompt_regex` 對 U-Boot 提示符、`ready_probe: "echo __READY__${nonce}"`、login 退化。
- [ ] 3.2 確認 line-command 路徑不被 bootloader 狀態硬擋，且與 #44 recovery lease 並存（必要時補測試）。
- [ ] 3.3 真機驗證（COM1）：確認板子可進 U-Boot prompt、調整 `prompt_regex` 至實機字串；`cmd submit --selector COM1 --cmd 'printenv' --mode line` 框出輸出、session 為 READY。記錄驗證證據。

## 4. #53 真實鍵入時間追蹤

- [ ] 4.1 RED：fake-PTY 測試——human owner 真實鍵入更新 `last_human_input_at`；broker probe RX 與 agent 注入不更新；`snapshot()` 可讀到。
- [ ] 4.2 GREEN：`uart_io` 在 `_handle_console_rx` human-owner 路徑記 `last_human_input_at`，`snapshot()` 暴露。
- [ ] 4.3 確認測試轉綠。

## 5. #53 human_active 語意與 self-test 欄位

- [ ] 5.1 RED：測試 `self_test`/`_lease_context` 新增 `human_active`（lease 在且 ≤60s→True；>60s→False；無 lease→False），且 `human_attached` 語意不變；idle 相關 `recommended_action` 改看 `human_active`。
- [ ] 5.2 GREEN：`_lease_context` 加 `human_active`（用 `HUMAN_ACTIVE_WINDOW_S=60s` 常數）；self_test 輸出帶 `human_active`；idle gate 改看 `human_active`。
- [ ] 5.3 確認測試轉綠（含既有 `human_attached` 契約測試不退化）。

## 6. #53 soft preempt + liveness

- [ ] 6.1 RED：測試——human lease idle(>60s) 時 agent 取得 owner 走 soft preempt（human 降級、鍵入進 deferral、agent 結束回放、console 不中斷）；human active(≤60s) 時 agent 不奪 owner。
- [ ] 6.2 RED：測試——死孤兒（`console_has_external_peer`=False）在 self-test/acquire 前被 detach + 關 lease；alive-idle 不被自動 detach。
- [ ] 6.3 GREEN：在 agent 取得控制權路徑接上 idle→soft preempt（沿用 `suspend_interactive`/`resume_interactive`/`_deferred_buffers`）；確保 acquire/self-test 前呼叫既有 liveness path。
- [ ] 6.4 確認測試轉綠；重點檢查位元組不交錯、回放正確、「agent 結束與 human 重新活躍同時」競態。

## 7. 文件與變更紀錄

- [ ] 7.1 README 補 `ATTACHED` vs `READY`、`command_capable`/`PROFILE_NOT_COMMAND_CAPABLE`、`uboot-template`、human_active/soft-preempt 說明（R-18 docs 對齊）。
- [ ] 7.2 更新 `CHANGELOG.md`（`[Unreleased]`）。
- [ ] 7.3 四份 agent 檔若無修改則不動（本 change 預期不改 agent 檔）。

## 8. 驗證與收尾

- [ ] 8.1 `python3 -m pytest -q tests/` 全套通過（無新失敗；注意兩支 pre-existing flaky）。
- [ ] 8.2 `python3 -m policy_check --repo .` 通過。
- [ ] 8.3 requesting-code-review；對每個 finding 走 receiving-code-review，修後 re-review。
- [ ] 8.4 openspec archive 該 change。
- [ ] 8.5 commit（Conventional Commits + trailer）→ push → 開 PR（body 填 Policy Checklist、`Closes #51`、`Closes #53`）。
