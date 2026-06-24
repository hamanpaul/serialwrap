# Changelog

本檔案依照 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/) 格式維護，版本依照 [Semantic Versioning](https://semver.org/lang/zh-TW/) 編號。

## [Unreleased]

### Fixed

- **`suspend_interactive`/`resume_interactive` 改為可重入，修正並發 agent 操作使 human 永久失去 raw ownership（#78）**：原本以單一 `_suspended_owner` 保存 owner，兩條 agent 路徑（`execute_command`／`file_push`/`pull`／`self_test`／interactive lease soft-preempt）重疊呼叫時，第二次 suspend 會把已保存的 `human:X` 覆寫成 `None`，resume 後 human 永久掉到 line-buffer/deferred（打字卡/掉字）。改以巢狀深度計數（`_suspend_depth`）：只有最外層 suspend 保存 owner、最外層 resume 才還原並 flush deferred；不平衡 resume 為 no-op；被 suspend 的 human console 斷線則重置深度。新增 `tests/test_suspend_resume_reentrant.py`。
- **systemd-system 模式可在 pipx 使用者安裝下實際啟動（#76）**：原 system unit 寫死 `User=serialwrap` dedicated account 且 `ExecStart=/usr/local/bin/serialwrapd`，但 pipx 只把 serialwrapd 裝到安裝者家目錄 venv（`~/.local/bin/serialwrapd`）、且 install 從未建立 `serialwrap` 帳號 → `setup --system` 起不來。改為 **run-as-user**：`render_system_unit(exec_start, run_user=...)` 參數化 `User=`，`setup_cmd` 以 `_resolve_run_user()`（`SUDO_USER`→`getpass.getuser()`）帶安裝者本人帳號、`_resolve_serialwrapd_path()` 解析其 pipx serialwrapd 絕對路徑組 ExecStart。保留「非 root + dialout」安全邊界，socket 仍於 `/run/serialwrap`（不受 WSLg `/run/user` 遮蔽、不依賴脆弱的 `user@<uid>`）。

### Added

- **`serialwrap setup` 在 WSL 上主動啟用 systemd（#76）**：新增 `ensure_wsl_systemd()`——偵測到 WSL 但 systemd 尚未啟用時，合併寫入 `/etc/wsl.conf` `[boot] systemd=true`（保留既有段落／鍵，經 staging + `sudo install`，不破壞性丟棄無法解析內容），並早退提示使用者於 Windows 端 `wsl --shutdown` 重啟後再跑 `setup --system`。非 WSL 或 systemd 已啟用 → no-op。新增 `tests/test_wsl_systemd.py` 與 system unit run-as-user 測試。


### Added

- **`serialwrap setup` / `doctor` 子命令接線（`feature/install-flow-systemd-pipx`，T12）**：新增 `sw_core/doctor_cmd.py`（`run_doctor` 對 Python 版本／PyYAML／`serialwrap`+`serialwrapd` 是否在 PATH／`dialout` 群組／systemd／監管模式／by-id 裝置／WSL systemd 做唯讀診斷，每項永不拋例外並附繁中修復提示；systemd／wsl_systemd／devices 為 advisory 不拉低整體 ok）；`sw_core/setup_cmd.py` 新增 `detect_legacy_install`（偵測舊版 `~/.paul_tools/serialwrap` 安裝並回報 minicom 符號連結／`/tmp/serialwrap/state.json` 殘留與退役指引，只指引不刪除）；`sw_core/cli.py` 註冊 `setup`（`--user`/`--system`/`--on-demand` 互斥目標模式、`--force`、`--with-sudo`）與 `doctor` 子命令並接上 `materialize_assets`＋`reconcile`（daemon/flash 偵測為 best-effort try/except 預設 False，不阻擋 setup；`FlashingBusy` → `FLASHING_BUSY` rc 2），同步重生 `README.md` `serialwrap-help` marker（R-16）。
- **修整 `reconcile` 對真實 `SystemEffects` 的相容性（T12 整合時發現）**：`reconcile` 回傳的 `ran` 欄位原直接讀 `fx.calls`（僅 `FakeEffects` 測試替身有此屬性），接上真實 `SystemEffects` 時會 `AttributeError`；改以 `getattr(fx, "calls", [])`，真實路徑回空 `ran`、`FakeEffects` 行為不變。
- **安裝流程重構為 pipx + systemd（進行中，`feature/install-flow-systemd-pipx`）**：新增 `pyproject.toml`（setuptools，console_scripts `serialwrap`/`serialwrapd`、宣告 `PyYAML`、`requires-python>=3.10`、資產 relocate 進 `sw_core/assets/` 並以 `importlib.resources` 取用）；新增 `serialwrap setup`/`doctor`/`service` 子命令；daemon 生命週期改以 systemd 服務為主（`systemd-user` 預設、`--system` 選項），無 systemd 平台退回現有 on-demand spawn 降級備援，並以 `config.yaml` 的 `supervision_mode` 為單一事實來源 gate 掉 auto-spawn 競態；路徑改 XDG（脫離 `/tmp`）並保留所有 `SERIALWRAP_*` 覆寫；minicom wrapper 改以 `command -v minicom` 解析；向後相容遷移既有 `~/.paul_tools` 安裝。設計見 `docs/superpowers/specs/2026-06-22-install-flow-systemd-pipx-design.md`，OpenSpec 變更 `openspec/changes/install-flow-systemd-pipx/`。
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

- **agent 檔改為單一事實來源 + symlink**：`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向 `CLAUDE.md` 的 symlink，往後只維護 `CLAUDE.md`；原 `.github/copilot-instructions.md` 專屬的「實際命令／高層架構／關鍵慣例／測試與除錯重點」併入 `CLAUDE.md`（操作事實對齊現行 pipx + systemd + XDG 流程）。policy_check R-13（`is_file()`）/ R-14（`read_text()` 找 `policy_version`）跟隨 symlink 解析至 `CLAUDE.md`，仍合規 v1.0.5。
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

### Fixed

- **bulk 傳檔不再凍結共用 daemon 的所有 RPC（#52）**：`file.push`/`file.pull` 過去在單執行緒 asyncio event loop 內**同步**執行整段 UART base64 分段傳輸（含阻塞式 `wait_for_regex_from` 輪詢），傳輸期間 `JsonRpcUnixServer` 無法服務任何其他連線，導致所有 COM 的 `cmd submit`／`session *`／`file *`／RPC-mediated interactive console 全部凍結（真機實測：一次 `health.ping` 被卡 **19.8s**＝整段傳輸時間）；reporter 當初量到「控制面 0.045s」是閒置時量的。修法（B-lite）：`JsonRpcUnixServer` 新增 `blocking_methods` 參數，`serialwrapd` 以 `BLOCKING_RPC_METHODS = {"file.push", "file.pull"}` 把這兩個長阻塞 handler 丟到 executor 執行（`loop.run_in_executor`），event loop 於傳輸期間維持可服務其他 RPC；CLI 介面與同步語意完全不變。**真機驗證**：傳輸進行中 40 次 `health.ping` 全部 **0–1ms**（修前 19.8s）；資料面 raw console（走 `_loop` 執行緒、繞過 RPC）本就不受影響（傳輸期間最差 gap 86ms）。已知限制：同一 session 並發兩個傳輸在罕見情況可能各自回 `CHECKSUM_MISMATCH`（已偵測、非靜默損壞），列為後續強化。
- **#69 開機窗自動重探**：daemon 針對 `ATTACHED/PROMPT_UNAVAILABLE` 與 `DETACHED/*_PROMPT_TIMEOUT` 等可復原非 READY session，於 RX 閒置後以有界 backoff 自動重跑既有 readiness probe；公開 `reprobe_attempts` / `next_reprobe_at` / `reprobe_exhausted`，並讓 `minicom_router.sh` 在 DUT 開機窗提示自動重探與手動 `session recover` 路徑。
- **#69 對抗式審查（codex adversarial-review）修正**：(1) `UARTBridge.send_bytes` 於 `_flash_mode` 期間丟棄非 `flash` 來源寫入，堵住 reconcile/probe 在 FLASHING 競態下寫 bytes 汙染 SBL 串流的真正 choke point；auto worker 寫 probe 前在 lock 內再驗證狀態（Finding 1）。(2) 新增 per-session probe 互斥鎖，auto 自動重探與 manual `session attach`/`session recover` 不再並發 probe 同一 bridge（Finding 2）。(3) ATTACHED readiness probe 改背景 worker 執行（`reconcile_readiness` 不再同步阻塞 DeviceWatcher tick／延誤裝置偵測；新增 `join_reprobe_workers()`，Finding 3）。(4) 手動 `session recover` 視為顯式介入，先重置 reprobe 上限/進度（re-arm），避免 exhausted 的 ATTACHED session 在手動 recover 仍失敗後永遠被 reconcile 跳過（Finding 4）。(5) 失敗重探的 backoff 改以 probe「完成時間」計算 `next_reprobe_at`（而非開始時間），避免慢速/靜默開機下 probe 阻塞超過 backoff 間隔時 `next_reprobe_at` 落在過去、下一 tick 立刻重探、過早 `reprobe_exhausted`（Finding round6）。
- **FLASHING 狀態完整性硬化（#69 對抗式審查 r2–r5，含既有 gap）**：(a) `send_bytes` flash gate 與 `set_flash_mode` 共用 `_write_lock`（鎖序 `_write_lock ⊃ _state_lock`），使「檢查 flash_mode → 寫入」原子化，杜絕 flash 開啟插進空隙；flash 寫入授權改用內部能力 `_allow_during_flash`（僅 `flash_tx`），不再綁可偽造的 `source` 字串。(b) `exit_flashing` 不論 state 一律 `set_flash_mode(False)`（防禦縱深），並為 `_probe_existing_bridge`/`_transition_to_attached`/reconcile reprobe except／reboot-recovery 迴圈加 FLASHING/RELEASED guard，杜絕競態路徑把 FLASHING 打回 ATTACHED 導致 bridge 永久卡 flash 模式。(c) `clear_session` / `release_device` / `bind_session` / `recover --force` 在 FLASHING 期間一律回 `FLASHING_BUSY`、不 detach bridge，避免並發管理操作切斷進行中的 MCU 燒錄 transport（既有 gap，比照 cmd submit 既有 FLASHING_BUSY 守法）。
- 修正 broker minicom 正常或非 0 結束時可能因 shell 被 `exec` 取代而跳過 `session console-detach` 的 lifecycle cleanup 風險。
- **device handoff（#54）C1**：修正飛行中的 attach 在 `bridge.start()`+probe 窗口期間遇到 `release_device` 時，會在最終 commit 把 `RELEASED` 覆寫成 `READY`/`ATTACHED` 並重開燒錄中裝置 raw FD 的 race；`_attach_by_id` 與 `_attach_by_id_dynamic` 兩段防護：設 `ATTACHING` 前 RELEASED 早退（常見情形不開 FD），最終 commit 前加 RELEASED backstop（關掉剛開的 FD、保留 `RELEASED` 不打回 `DETACHED`）。
- **device handoff（#54）C2**：`attach_session` / `recover_session` 對 RELEASED session 改為比照 `clear_session` 早退（回 `released=True` + `recommended_action: device_attach`），不再把 session 打成 wedged-`ATTACHING`；同時避免下一次 `_save_state` 因 `state != "RELEASED"` 把 released map 寫空，確保重啟後 RELEASED 保護不失、bootstrap 不搶回裝置。
- **device handoff（#54）I1**：`bind_session` 對 RELEASED session 重綁新 by_id 時，先把舊 by_id 移出 `_released_by_ids` 並清 provenance（`released_by`/`released_at`/`released_reason`），避免舊 by_id 永久殘留集合。
- **device handoff（#54）I2**：`self_test` 的 RELEASED 早退分支改為在 `self._lock` 內擷取 `real_path`/provenance/`to_public_dict()`，出 lock 後才呼叫 `_probe_external_holder`（掃整個 /proc），避免持 lock 期間阻塞所有 RPC。
- **device handoff（#54）PR review 修正**：`device.attach` 對「已 attached 且非 RELEASED」的 session 改為冪等回覆 `already_attached`，不再誤設 `ATTACHING` 導致 `_attach_by_id` 早退而卡死；`_probe_external_holder` 加上 device number（`st_rdev`）比對，外部即使以 `/dev/serial/by-id/...` 等 symlink 開啟裝置也能偵測到，避免 `device.attach` 誤判可收回而重回 two-reader race；`to_public_dict` 補 `released_reason`。

### Security

- 配合 `tier: shareable` 的 R-21（已對公開廠商/OS 名減敏），清除 tracked 內容中的個人家目錄絕對路徑（docs/skills 改 `~/`、func-test 改 `/tmp/` 以維持可攜）與雇主裝置型號（改用公開的 `Broadcom CFE 平台` 通稱）；停止追蹤 ctags 索引檔（含絕對路徑）並加入 `.gitignore`。
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
