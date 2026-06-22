# Changelog

本檔案依照 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/) 格式維護，版本依照 [Semantic Versioning](https://semver.org/lang/zh-TW/) 編號。

## [Unreleased]

### Added

- 四份 agent 檔（`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.github/copilot-instructions.md`）新增「MCU flash 真機驗證手法（#55 `/dev/ttyMCU`）」段：throwaway daemon + FTDI-only sandbox `SERIALWRAP_BY_ID_DIR` 隔離跑法、DUT console GPIO BSL-invoke（長指令需逐行）、以及三個只有實機才現形的坑（idle 汙染／double-sync 吃 ACK／PTY 無 EOF 卡 FLASHING）。
- **MCU 韌體升級 flash 端點 `/dev/ttyMCU`（#55）**：daemon 持續 maintain tty（維持 real device 唯一 reader、無 two-reader race）下提供 byte-transparent flash 端點，外部 flasher（如 `ocp-mcu-upgrade -d /dev/ttyMCU`）原生走通燒錄。以非破壞性 **sync-probe** 自動認 MCU 線（偵測階段排除 `command_capable` console，避免燒到 DUT；多候選 ACK → 不自動挑，狀態經 `mcu status` 的 `last_detect` 呈現），不依賴會漂移的 `/dev/ttyUSBx`/by-id；FLASHING 期間封鎖 console / interactive 注入（`FLASHING_BUSY`），確保 SBL binary 不被汙染；可擴充 **pattern registry**（`sw_core/mcu_patterns.py`，預設 TI CC2674/CC2652 `55 55`→`00 cc`，非破壞不變式守於 `__post_init__`）；新增 `FlashEndpoint`（`sw_core/flash_endpoint.py`，常駐 PTY slave 設 raw 確保 byte-transparency、未 bridge 時一律沉默不主動寫入、偵測 client 寫入才進 flash、雙向 pump；清單查詢走 `mcu patterns`/`mcu status` 不經此 PTY——真機實證 idle 寫入會汙染 flasher sync）；`UARTBridge.flash_tx`（原樣 TX 跳過行處理）+ baud termios 鏡射；session `FLASHING` 狀態（不 detach，`cmd submit` 回 `FLASHING_BUSY`，結束自動恢復 console）；`mcu patterns` / `mcu status` RPC+CLI 與 MCP tool。全程 RAW WAL 留證。設計／計畫見 `docs/superpowers/specs/2026-06-17-mcu-fw-upgrade-flash-broker-design.md`、`docs/superpowers/plans/2026-06-17-mcu-fw-upgrade-flash-broker.md`。**真機 gate 已通過**：OCTOPUS/CC2674（FTDI as COM0）經 `/dev/ttyMCU` 完整實燒 `Return error code : 0x0`（APP+CCFG erase/write/CRC）、燒後 session 自動恢復 `ATTACHED`。真機過程另修三個只有實機現形的問題：idle 清單寫入汙染 flasher（改為端點一律沉默）、獨立 probe 吃掉 MCU sync ACK 的 double-sync（改用 flasher sync 並回放 ACK）、flasher 斷線因 PTY 無 EOF 卡 FLASHING（改用 holder-probe 偵測斷線收尾）。
- `device release` / `device attach`（RPC `device.release` / `device.attach`）：把單一 session 綁定的 UART 乾淨交給外部 flasher 並可手動收回（#54）；新增 `RELEASED` 狀態、`_spawn_attach` released guard、跨 daemon 重啟持久化、`self_test` 的 `external_holder`/`reclaimable` 標註與 `device attach` 安全 guard（`DEVICE_STILL_HELD`，`--force` 可略過）
- 新增 device release / handoff 設計文件 `docs/superpowers/specs/2026-06-15-device-release-handoff-design.md`、OpenSpec change（已封存於 `openspec/changes/archive/2026-06-16-device-release-handoff/`，含 proposal/design/specs/tasks）與實作計畫 `docs/superpowers/plans/2026-06-15-device-release-handoff.md`（#54：daemon 持續運作下把單一 device 交給外部 flasher、燒完手動收回的 surgical release 機制）
- 新增 `.github/workflows/tests.yml`：在 PR 上以 `python3 -m pytest -q tests/` 執行測試套件（修補 R-19：repo 有 `tests/` 但無 CI 執行測試）。
- **command_capable 判準與 `PROFILE_NOT_COMMAND_CAPABLE` 錯誤碼（#51）**：以 `command_capable = bool(profile.ready_probe.strip())` 判定 session 可否下命令，取代以 `platform == "passthrough"` 寫死；非 command-capable（無 ready_probe，含 passthrough / others-template 等僅 console 的 profile）在 `ATTACHED` 下 `cmd submit` 改回語意明確的 `PROFILE_NOT_COMMAND_CAPABLE`（附 hint），不再回易誤解的 `SESSION_NOT_READY`；`to_public_dict()` 新增 `command_capable` 欄位。
- **`self_test` 在最外層 result dict 暴露 `command_capable`（#51）**：所有分類分支（含 `SESSION_NOT_FOUND` 早退、`RELEASED`、`ATTACHED`/`READY`/`OK` 等）的最外層 result 都帶 `command_capable`，呼叫端不必鑽進巢狀 `"session"` dict；查無 session 時為 `False`。
- **新增 `uboot-template` profile（#51）**：`profiles/default.yaml` 增加 bootloader 導向的 command-capable profile（`platform: passthrough`、`prompt_regex` 匹配 `=>` / `u-boot>` / `CFE>`、`ready_probe: echo __READY__${nonce}`），讓停在 U-Boot prompt 的 session 能進 `READY` 並接受 line command；`ProfileTemplate` 比照 `SessionProfile` 新增 `command_capable` property。
- **追蹤 human 真實鍵入並新增 `human_active` 時間窗語意（#53）**：`UARTBridge` 新增 `last_human_input_at`（僅在 human-OWNER 的直接 raw 送出分支更新，deferred buffer／serial RX loop／agent 注入皆不更新）並由 `snapshot()` 暴露；`sw_core/constants.py` 新增 `HUMAN_ACTIVE_WINDOW_S = 60.0` 時間窗常數；`self_test` 結果在最外層新增 `human_active`（僅在 `human_attached` 且最後鍵入仍在時間窗內為 `True`），讓「人類已 attach 但長時間 idle」的 lease 不再被當成正在使用。`human_attached` 語意維持不變（#53 issue groundwork）。
- **閒置 human lease 可被 agent soft preempt（#53）**：`interactive_open` 在 READY 路徑遇到既有 human lease 但 `human_active=False` 時，改以 suspend + stash 將 human **降級**（console 不中斷、其鍵入進 deferred buffer，agent 關閉 lease 後還原並回放）取得控制權，回傳 `soft_preempted`；human 仍 active 或既有為 agent lease 維持 `SESSION_INTERACTIVE_BUSY`。死孤兒（console peer 已關）沿用既有 liveness 在 `self_test` 時 detach；活著但 idle 的 console 只降級、不自動 detach（清理交由 agent 主動 `recover`/`console-detach`）。解決孤兒 minicom 假性佔用 console 導致 co-work 卡住的問題。

### Changed

- agent skill 整併為 repo 內唯一權威來源 `skills/serialwrap/SKILL.md`（CLI-first，改名 `serialwrap-mcp` → `serialwrap`），`install.sh` symlink 到 `~/.agents/skills/`；移除 root `skills.md`。(#59)
- 移除 `Dockerfile` 對已退役 `serialwrap-mcp` 的 `chmod +x`（該檔已隨 MCP 退役刪除，否則 `docker build` 及 `tools/docker/remote_smoke.sh` remote smoke 會因檔案不存在而失敗）。(#59)
- 文件與現行架構對齊：刪除 `sills.md` 轉址 stub；`docs/serialwrap-spec.md` 降級為概覽並指向 `openspec/specs/*`；`docs/plan.md`/`docs/todos.md` 標為歷史快照；`README.md` 狀態機補 `RELEASED`(#54)/`FLASHING`(#55)；`skills.md` 加 #59 cross-ref 與過時標記。
- 升級 policy conventions v1.0.4 → v1.0.5（pin 重釘到 `484f963a…`）：`.paul-project.yml`、四份 agent 檔（marker/policy_version/install·pinned SHA）、`.github/workflows/policy-check.yml`（uses/policy_engine_ref/policy_version）一併同步。新增 R-22 doc_reference 於本地與 CI 生效。
- **command_capable 改以 ready_probe 為準（#51）**：`_attach_by_id` / `_attach_by_id_dynamic` / `_probe_existing_bridge` 不再以 `platform == "passthrough"` 寫死 `ok=False`，改以 `profile.command_capable` 判定——有設 `ready_probe` 的 target（含 passthrough）能走正常 probe 進 `READY`，無 `ready_probe` 者維持 `ATTACHED`。

- **同步 policy 1.0.1**：`policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` + 四份 agent 檔 + `managed-by@v1.0.1` + agent 檔內 engine pinned SHA），caller `policy-check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17 / R-18 與語言規範說明
- `tools/minicom_router.sh` 的 broker minicom 自動 transcript 預設改為 `script -qef` wrapper，不再預設把 `-C` 傳給 minicom；新增 `MINICOM_CAPTURE_MODE=script|minicom|off` 控制模式，`MINICOM_CAPTURE_MODE=minicom` 才明確 opt-in 使用原生 capture。
- **採用 policy 1.0.4**：`policy_version` 1.0.1 → 1.0.4（`.paul-project.yml` + 四份 agent 檔 + workflow `uses:`/`policy_engine_ref` 重釘至 `hamanpaul/paulsha-conventions@v1.0.4`，SHA `77a3e83`）；`.paul-project.yml` 宣告 `tier: shareable`。
- **強化 CLI help（`sw_core/cli.py`）**：為全部命令群組（`daemon`/`device`/`session`/`alias`/`cmd`/`stream`/`log`/`file`/`wal`/`event`）補上 `help=` 摘要與 `description=`，並為每個子命令（含先前看不到的 `recover`/`self-test`/`release`/`attach` 等）補繁中 `help=`；子命令選單 `metavar` 由冗長的 `{...}` 改為 `<group>`／`<command>`，`event` 既有英文 help 一併改為繁中；同步重生 `README.md` `## Usage` 的 `serialwrap-help` marker 區段（R-16）。純說明文字調整，不影響任何指令行為與參數。

### Removed

- 退役 vestigial MCP 層：刪除 `sw_mcp/`（含 server.py）與 `serialwrap-mcp` shim，並移除/改寫 4 個 MCP-coupled 測試（event/remote/bootloader 改走 CLI/RPC 路徑覆蓋）。(#59)

### Security

- 配合 `tier: shareable` 的 R-21（已對公開廠商/OS 名減敏），清除 tracked 內容中的個人家目錄絕對路徑（docs/skills 改 `~/`、func-test 改 `/tmp/` 以維持可攜）與雇主裝置型號（改用公開的 `Broadcom CFE 平台` 通稱）；停止追蹤 ctags 索引檔（含絕對路徑）並加入 `.gitignore`。

### Fixed

- **#69 開機窗自動重探**：daemon 針對 `ATTACHED/PROMPT_UNAVAILABLE` 與 `DETACHED/*_PROMPT_TIMEOUT` 等可復原非 READY session，於 RX 閒置後以有界 backoff 自動重跑既有 readiness probe；公開 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted`，並讓 `minicom_router.sh` 在 DUT 開機窗提示自動重探與手動 `session recover` 路徑。
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
