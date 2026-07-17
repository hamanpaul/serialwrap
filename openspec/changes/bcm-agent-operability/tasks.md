## 1. #140 帳密解析狀態（auth 層）

- [ ] 1.1 `sw_core/auth.py`：新增 `AuthResolution`（`reason` enum：`ok`/`env_file_missing`/`env_file_unreadable`/`key_absent`/`not_configured` + 實際解析 env_file 絕對路徑）；`resolve_session_auth` 回 `(SessionAuth, AuthResolution)`，判定各 reason（含 env_file 存在但缺 key/值空）
- [ ] 1.2 更新既有 `resolve_session_auth` 呼叫端相容（tuple 解包或 helper），確認 login_fsm/session_manager 取得 SessionAuth 不破壞
- [ ] 1.3 unit：`tests/` 覆蓋五種 reason（含帶實際路徑）與 SessionAuth 值；先寫 failing（TDD）

## 2. #140 login 不送空帳密 + CREDENTIALS_UNRESOLVED

- [ ] 2.1 `sw_core/login_fsm.py` + `sw_core/session_manager.py`：login prompt 命中、profile 宣告帳密來源（user_env/pass_env/env_file 任一）但 `AuthResolution.reason != ok` → 不送空 user/pass、中止登入、set `last_error=CREDENTIALS_UNRESOLVED`
- [ ] 2.2 `CREDENTIALS_UNRESOLVED` 為終態：reprobe/自動重探不再送空帳密（找 reprobe 路徑加 gate）
- [ ] 2.3 一次性清楚警告（log + WAL 事件），含 env_file 實際解析絕對路徑與 reason，不印帳密值
- [ ] 2.4 未宣告帳密來源（not_configured）行為不變的守衛
- [ ] 2.5 unit：宣告帳密但空 → 不送空、回 CREDENTIALS_UNRESOLVED、終態不重試；not_configured 行為不變（TDD 先紅）
- [ ] 2.6 PTY fake-target E2E（sandbox，非 tests/）：假 Login: + 缺 env_file → session 停 CREDENTIALS_UNRESOLVED、WAL 零空帳密 TX、log 有路徑警告

## 3. #114 allow-attached lease 授予條件擴充

- [ ] 3.1 `sw_core/session_manager.py` `interactive_open` allow_attached 分支（約 :3065）：gate 改為 `_matches_any_bootloader_prompt(...) OR detect_boot_banner(rx_tail)`（複用 #130 `detect_boot_banner`/`BOOT_BANNER_PATTERNS`）；banner 命中授予 recovery lease 並於回應標 `boot_interrupt: True`，prompt 命中維持現有回應
- [ ] 3.2 確認 banner 命中所開 lease 與 prompt 命中一致（recovery_mode/owner/human stash-restore）；`boot_interrupt` 為 additive 欄位
- [ ] 3.3 unit：倒數行/banner 命中 → 授予 + boot_interrupt=True；`=>` prompt 命中 → 授予、無 boot_interrupt；皆不命中 → NOT_BOOTLOADER；READY 下不檢查（TDD 先紅）
- [ ] 3.4 PTY fake-U-Boot E2E（sandbox）：倒數窗 `interactive-open --allow-attached` 成功（boot_interrupt=True）→ `interactive-send` 送鍵 → 假 target 轉 `=>`、lease 仍持有

## 4. 文件與 changelog

- [ ] 4.1 README（中英）：`CREDENTIALS_UNRESOLVED` 排查（含 env_file 相對 daemon profile-dir 解析、非 XDG）、`interactive-open --allow-attached` 倒數窗中斷用法與 `boot_interrupt`
- [ ] 4.2 `docs/serialwrap-spec.md`、`sw_core/assets/skill/SKILL.md` 同步
- [ ] 4.3 `changelog.d/140-credentials-unresolved.md`（type: fix）、`changelog.d/114-autoboot-interrupt-lease.md`（type: feat）

## 5. 驗證與收斂

- [ ] 5.1 `python3 -m pytest -q tests/`（排除 6 個 PTY-heavy flaky 檔）全綠、無新失敗
- [ ] 5.2 `python3 -m policy_check --repo .` 通過（帶 PR 上下文複現 CI）
- [ ] 5.3 對抗式 review（獨立 agent）→ findings 收斂
- [ ] 5.4 grep .md 無工具標記殘留；commit 帶三行 trailer
- [ ] 5.5 openspec archive；PR body `Closes #114`、`Closes #140`
