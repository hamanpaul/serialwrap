# Changelog

本檔案依照 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/) 格式維護，版本依照 [Semantic Versioning](https://semver.org/lang/zh-TW/) 編號。

## [Unreleased]

### Added

- `device release` / `device attach`（RPC `device.release` / `device.attach`）：把單一 session 綁定的 UART 乾淨交給外部 flasher 並可手動收回（#54）；新增 `RELEASED` 狀態、`_spawn_attach` released guard、跨 daemon 重啟持久化、`self_test` 的 `external_holder`/`reclaimable` 標註與 `device attach` 安全 guard（`DEVICE_STILL_HELD`，`--force` 可略過）
- 新增 device release / handoff 設計文件 `docs/superpowers/specs/2026-06-15-device-release-handoff-design.md`、OpenSpec change（已封存於 `openspec/changes/archive/2026-06-16-device-release-handoff/`，含 proposal/design/specs/tasks）與實作計畫 `docs/superpowers/plans/2026-06-15-device-release-handoff.md`（#54：daemon 持續運作下把單一 device 交給外部 flasher、燒完手動收回的 surgical release 機制）
- 新增 `.github/workflows/tests.yml`：在 PR 上以 `python3 -m pytest -q tests/` 執行測試套件（修補 R-19：repo 有 `tests/` 但無 CI 執行測試）。

### Changed

- **同步 policy 1.0.1**：`policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` + 四份 agent 檔 + `managed-by@v1.0.1` + agent 檔內 engine pinned SHA），caller `policy-check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17 / R-18 與語言規範說明
- `tools/minicom_router.sh` 的 broker minicom 自動 transcript 預設改為 `script -qef` wrapper，不再預設把 `-C` 傳給 minicom；新增 `MINICOM_CAPTURE_MODE=script|minicom|off` 控制模式，`MINICOM_CAPTURE_MODE=minicom` 才明確 opt-in 使用原生 capture。
- **採用 policy 1.0.4**：`policy_version` 1.0.1 → 1.0.4（`.paul-project.yml` + 四份 agent 檔 + workflow `uses:`/`policy_engine_ref` 重釘至 `hamanpaul/paulsha-conventions@v1.0.4`（SHA `77a3e83`）；`.paul-project.yml` 宣告 `tier: shareable`。

### Security

- 配合 `tier: shareable` 的 R-21（已對公開廠商/OS 名減敏），清除 tracked 內容中的個人絕對路徑（`/home/<user>/` → docs/skills 改 `~/`、func-test 改 `/tmp/` 以維持可攜）與雇主裝置型號（`BGW720` → `Broadcom CFE 平台`）；停止追蹤 ctags 索引 `tags-session-manager`（含絕對路徑）並加入 `.gitignore`。

### Fixed

- 修正 broker minicom 正常或非 0 結束時可能因 shell 被 `exec` 取代而跳過 `session console-detach` 的 lifecycle cleanup 風險。
- **device handoff（#54）C1**：修正飛行中的 attach 在 `bridge.start()`+probe 窗口期間遇到 `release_device` 時，會在最終 commit 把 `RELEASED` 覆寫成 `READY`/`ATTACHED` 並重開燒錄中裝置 raw FD 的 race；`_attach_by_id` 與 `_attach_by_id_dynamic` 兩段防護：設 `ATTACHING` 前 RELEASED 早退（常見情形不開 FD），最終 commit 前加 RELEASED backstop（關掉剛開的 FD、保留 `RELEASED` 不打回 `DETACHED`）。
- **device handoff（#54）C2**：`attach_session` / `recover_session` 對 RELEASED session 改為比照 `clear_session` 早退（回 `released=True` + `recommended_action: device_attach`），不再把 session 打成 wedged-`ATTACHING`；同時避免下一次 `_save_state` 因 `state != "RELEASED"` 把 released map 寫空，確保重啟後 RELEASED 保護不失、bootstrap 不搶回裝置。
- **device handoff（#54）I1**：`bind_session` 對 RELEASED session 重綁新 by_id 時，先把舊 by_id 移出 `_released_by_ids` 並清 provenance（`released_by`/`released_at`/`released_reason`），避免舊 by_id 永久殘留集合。
- **device handoff（#54）I2**：`self_test` 的 RELEASED 早退分支改為在 `self._lock` 內擷取 `real_path`/provenance/`to_public_dict()`，出 lock 後才呼叫 `_probe_external_holder`（掃整個 /proc），避免持 lock 期間阻塞所有 RPC。
- **device handoff（#54）PR review 修正**：`device.attach` 對「已 attached 且非 RELEASED」的 session 改為冪等回覆 `already_attached`，不再誤設 `ATTACHING` 導致 `_attach_by_id` 早退而卡死；`_probe_external_holder` 加上 device number（`st_rdev`）比對，外部即使以 `/dev/serial/by-id/...` 等 symlink 開啟裝置也能偵測到，避免 `device.attach` 誤判可收回而重回 two-reader race；`to_public_dict` 補 `released_reason`。

### Security

- 將 `profiles/brcm.env`（含 `BRCM_USER` / `BRCM_PASS`）改為 `profiles/brcm.env.example` 範本並停止追蹤；`.gitignore` 新增 `profiles/*.env`，避免本機憑證 profile 被提交。實際憑證由使用者複製範本後在本機填入（`profiles/default.yaml` 的 brcm-template 仍以 `env_file: brcm.env` 載入）。

## [0.1.0] - 2026-05-15

### Added

- 導入 [paulsha-conventions](https://github.com/hamanpaul/paulsha-conventions) v1.0.0 治理基線（`.paul-project.yml`、`policy_version: 1.0.0`）
- 新增 `VERSION` 檔案並將正式 release 版本更新為 `0.1.0`
- 新增 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`（AI agent policy checklist）
- 新增 `.github/pull_request_template.md`（含 R-11 policy checklist）
- 新增 `.github/workflows/policy-check.yml`（PR 自動 policy 驗證）
- README.md 補充 `## Install`、`## Usage`、`## Version` 段落與 CLI help marker
- `sw_core/config.py`：`ProfileTemplate` 與 `SessionProfile` 均新增 `bootloader_prompts: tuple[str, ...]`（預設 `()`，immutable）；loader 從 YAML list 解析時只保留 `str` 元素，並傳播至 session profile
- `sw_core/constants.py`：新增 `MAX_RECOVERY_LEASE_S = 120.0` 與 `BOOTLOADER_RX_TAIL_BYTES = 512`（為 Issue #44 bootloader recovery 所需）
- `profiles/default.yaml`：`brcm-template` 加入 `bootloader_prompts`（CFE、U-Boot、BCM 系列 prompt pattern）
- `sw_core/session_manager.py`：新增模組層級 helper `_matches_any_bootloader_prompt`，以 RX tail 最後一個非空行比對 profile bootloader_prompts regex list（Issue #44 Phase B）
- `sw_core/session_manager.py`：`session.self_test` ATTACHED 路徑在 passthrough / LOGIN_REQUIRED / REBOOTING 之後、ATTACHED_NOT_READY 之前，新增 BOOTLOADER classification；命中時回傳 `matched_prompt`、`rx_tail`、`recommended_action: recover_interactive`
- `sw_core/session_manager.py`：`InteractiveLease` 新增 `recovery_mode: bool = False` 與 `suspended_human: bool = False` schema 欄位（Phase B 基礎，後續 interactive_open allow_attached/stash 使用）
- `sw_core/session_manager.py`：`SessionRuntime` 新增 `_stashed_human_lease: InteractiveLease | None = None`（Phase B 基礎，不透出 RPC）
- `sw_core/session_manager.py`：`_lease_context()` 新增 `recovery_mode` 欄位（所有 self_test 分類結果均含此欄位）
- `sw_core/session_manager.py`：`interactive_open(allow_attached=True)` 支援 ATTACHED 狀態下通過 bootloader prompt 比對後開啟 recovery lease；human lease 自動暫停（stash），close 時恢復
- `sw_core/session_manager.py`：recovery lease timeout 受 `MAX_RECOVERY_LEASE_S`（120s）clamp
- `sw_core/session_manager.py`：`interactive_open` / `interactive_status` 回傳 `recovery_mode` 欄位
- `sw_core/session_manager.py`：`_PostCloseAction` 機制保證 `bridge.resume_interactive()` 在 `_lock` 外執行
- `sw_core/session_manager.py`：`_detach_session_locked` 清除 `_stashed_human_lease`
- `sw_core/session_manager.py`：`_refresh_interactive_locked` 自動清除 expired 非 human lease，並透過 lock 外 post-close action 恢復 human deferred input
- `sw_core/service.py`：RPC `session.interactive_open` 透傳 `allow_attached` 參數
- `sw_core/cli.py`：`session interactive-open` 新增 `--allow-attached` 選項
- `sw_mcp/server.py`：`serialwrap_open_interactive` 工具 schema 新增 `allow_attached: boolean`
- `tests/test_bootloader_recovery.py`：新增 56 個 TDD 測試，涵蓋 recovery lease 完整生命週期（開啟、stash/restore、逾時 clamp、send、status recovery_mode、expired 清理、BUSY early-return post-close、detach 清除 stash、RPC/CLI/MCP 透傳）
- 新增 `docs/superpowers/specs/2026-05-15-release-repo-hygiene-design.md` 與本實作計畫，記錄 repo hygiene release 流程。

### Changed

- `.github/copilot-instructions.md` 前置 paulsha-conventions marker 與 policy_version
- `.gitignore` 新增 `test/reports/` 與常見本機測試、報告、log、coverage 產物規則。
- `.github/pull_request_template.md` 與 `docs/releases/v0.1.0.md` 補充 release PR 階段 label-aware policy check 與 post-tag plain policy check 的驗證時機。

### Removed

- 從目前 tracked tree 移除 `test/reports/` 測試報告；不重寫既有 git history。

### Fixed

- `sw_core/session_manager.py`：動態 auto-detect session 現在會從 `ProfileTemplate` 傳播 `bootloader_prompts`，避免未宣告 targets 的 template session 無法進入 BOOTLOADER recovery

### Security

- 掃描 tracked paths 與 tracked content，確認未發現需移除的非例外機敏資料；`profiles/brcm.env` 屬 console login profile 例外。

### Notes

- Phase A 為治理/文件/CI scaffolding，不含 Issue #44 recovery 功能
- Release PR 必須標記 `release:0.1.0`，讓 R-07 在 tag `v0.1.0` 建立前跳過
- policy_check engine pinned to `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
