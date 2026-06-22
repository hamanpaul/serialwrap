# Tasks — attach-boot-auto-reprobe（#69，serialwrap PR）

## 0. 真機複製（已完成，證據已入設計 §2）
- [x] COM1 reboot → 開機窗 attach → 卡 `ATTACHED/PROMPT_UNAVAILABLE` 70s+、不自動回 READY；self_test `ATTACHED_NOT_READY`；手動 attach 救不回、recover 才回 READY。板子已還原。

## 1. 常數 + session 欄位
- [ ] `sw_core/constants.py`：`REPROBE_RX_IDLE_S` / `REPROBE_BACKOFF_S` / `REPROBE_MAX_INTERVAL_S` / `REPROBE_MAX_ATTEMPTS`（或 `REPROBE_DEADLINE_S`）。
- [ ] `SessionRuntime` 新增 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted`；`to_public_dict()` 暴露；reset 條件（成功 READY / 人工 recover / device 變動時歸零）。

## 2. reconcile 週期工作（TDD）
- [ ] RED：unit 測試——構造一個 `ATTACHED`+`PROMPT_UNAVAILABLE`（及 `DETACHED`+`*_PROMPT_TIMEOUT`）的 session（fake bridge/probe），RX 轉閒後呼叫 reconcile → 期望觸發重探並轉 `READY`；RX 未閒 → 不重探；human-active/FLASHING/RELEASED → 跳過；達上限 → `reprobe_exhausted` 且停手。先看測試 fail。
- [ ] GREEN：在 `sw_core/session_manager.py` 新增 `reconcile_readiness()`（掃 sessions、套觸發條件、RX-idle gate、backoff/上限、複用 `_probe_existing_bridge`/`_spawn_attach`），以 session lock 去重避免與 recover/attach 重入。
- [ ] daemon 驅動點（`serialwrapd.py` 或 service periodic tick）週期呼叫 `reconcile_readiness()`。

## 3. minicom_router 訊息 + 可選 wait
- [ ] `tools/minicom_router.sh:~340`：session 非 READY 且 `last_error` 屬 prompt-timeout 類 / `reprobe_attempts>0` 時，輸出明確提示（DUT 可能開機中、serialwrap 自動重試中 / 手動 `serialwrap session recover --selector COMx`），取代 `broker not ready`。
- [ ] 可選 `MINICOM_WAIT_READY`：阻塞輪詢等待 READY（有上限）再開 minicom。
- [ ] `bash -n tools/minicom_router.sh`。

## 4. docs
- [ ] `README.md` / `skills/serialwrap/SKILL.md`：FAQ「開機窗連不到 → self-test 判讀 → 等自動重探 / 手動 recover」。

## 5. 驗證
- [ ] `python3 -m pytest -q tests/` 無新失敗（除既有 flaky）。
- [ ] `python3 -m policy_check --repo .` EXIT=0（R-09 CHANGELOG）。
- [ ] **真機驗證（COM1，設計 §6）**：reboot → 開機窗 attach → **不人工 recover**，觀察自動在 backoff 窗內回 `READY`（self_test OK、`reprobe_attempts` 有值）；minicom_router 開機窗內顯示新提示；還原板子、prod/其他 COM 不受影響。
- [ ] `CHANGELOG.md [Unreleased]` 記一筆。
