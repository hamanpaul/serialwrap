## Why

DUT 開機窗內 attach 時，probe 等不到 prompt → session 卡在非 READY（真機複製：`ATTACHED`+`PROMPT_UNAVAILABLE`；2026-06-18 報告：`DETACHED`+`LOGIN_PROMPT_TIMEOUT`），**且開機完成、prompt 已可用後仍不自動回 READY**，需人工 `recover`。根因：readiness probe 只在 attach 觸發當下跑一次（`login_fsm.ensure_ready`/`probe_ready`），daemon 主 loop 無週期性 re-probe（`uart_io._loop` 只 pump bytes）。詳見 `docs/superpowers/specs/2026-06-22-attach-boot-auto-reprobe-design.md`（含真機複製 §2、根因 §3）。

## What Changes

- **daemon 端有界自動重探（方案 A）**：新增 readiness reconcile 週期工作，對「卡在非 READY 但可復原」的 session（`ATTACHED`+prompt-unavailable 類 / `DETACHED`+prompt-timeout 類、device 在位、未 RELEASED/FLASHING、非 human-active）在 **RX 轉閒**（boot log 停＝prompt 可能已出現）時，以有界 backoff 重跑既有 probe，直到 `READY` 或達上限。
- 以 session 欄位（`reprobe_attempts`/`next_reprobe_at`/`reprobe_exhausted`）表達重試中，不新增 FSM 狀態；`to_public_dict()`/self_test 暴露。
- **minicom_router 訊息改善**：非 READY 且屬 prompt-timeout 類 / 正在自動重試時，輸出明確提示（取代 `broker not ready`），可選阻塞等待 READY。
- **docs/FAQ**：self-test 判讀 → 等自動重探 / 手動 recover 流程。

## Capabilities

### New Capabilities
- `session-readiness-recovery`: daemon 在 session 因 attach 撞開機窗而卡非 READY 時，於 prompt 可用後**有界自動重探回 READY**，無需人工介入；達上限才停手並回報明確狀態。

### Modified Capabilities
<!-- 無：不改 login_fsm 的 probe/login 判定行為與 command_capable（session-command-readiness）requirement，只新增「何時重跑 probe」。 -->

## Impact

- `sw_core/session_manager.py`（reconcile 週期工作 + session 欄位，複用既有 `_probe_existing_bridge`/`_spawn_attach`/probe）、`sw_core/constants.py`（`REPROBE_*` 常數）、daemon 驅動點（`serialwrapd.py`/service periodic tick）、`tools/minicom_router.sh`（訊息 + 可選 wait）、`README.md`/`skills/serialwrap/SKILL.md`（FAQ）。
- 觸及 code_paths → R-09 CHANGELOG 必記。
- 不改 capability **行為** 的既有 requirement（probe/login/command_capable 不動）；只新增自動重探。
- 真機驗證（COM1，user 同意的測試板）為驗收一環，見設計 §6。
