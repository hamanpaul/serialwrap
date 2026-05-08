# Tasks: ATTACHED / U-Boot fallback recovery + paulsha-conventions 導入（Issue #44）

## 1. Branch / scaffold

- [x] 1.1 從 `main` 開新 branch `feature/bootloader-recovery-44`
- [x] 1.2 確認 `python3 -m pytest -q tests/` 在改動前的綠燈狀態（baseline）
- [x] 1.3 Local 安裝 paulsha-conventions policy engine（pin SHA `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`）；先跑 `python3 -m policy_check --repo .` 把預期會 fail 的規則記下來（baseline）

## 2. paulsha-conventions Bootstrap（先做、避免後續 PR 反覆改）

- [x] 2.1 新增 `.paul-project.yml`（`policy_profile: flat`、`policy_version: 1.0.0`、`code_paths`、`cli`，內容見 design §7.2 / §7.3）
- [x] 2.2 新增 `VERSION`（單行 `0.0.1`，對齊既有 git tag `v0.0.1` / R-07）
- [x] 2.3 新增 `CHANGELOG.md`（Keep-a-Changelog 1.1.0；`[Unreleased]` 段落骨架）
- [x] 2.4 新增 `CLAUDE.md`（managed-by marker + policy_version: 1.0.0 + 在地化 checklist：本 repo 測試指令 `python3 -m pytest -q tests/`、policy_check 指令）
- [x] 2.5 新增 `AGENTS.md`（內容與 `CLAUDE.md` 相同，三份共用同一 managed-by marker / policy_version；R-13 只驗證檔案存在，R-14 掃 `^policy_version:` 裸行）
- [x] 2.6 新增 `GEMINI.md`（同上）
- [x] 2.7 既有 `.github/copilot-instructions.md` 首行加 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->` 與 policy_version: 1.0.0；保留其餘 11KB 既有內容
- [x] 2.8 grep 四份 agent file 的 policy_version 字串、確認皆為 `1.0.0`
- [x] 2.9 新增 `.github/pull_request_template.md`（conventions 標準 template + R-11 checklist）
- [x] 2.10 新增 `.github/workflows/policy-check.yml`，雙 pin 到 `hamanpaul/paulsha-conventions@ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
- [x] 2.11 補 README.md `## Install` / `## Usage` / `## Version` 段落（缺則加 heading + 一段引用，不重寫既有內容）；若 R-16 啟用，`## Usage` 加 `<!-- BEGIN: cli-help marker="serialwrap-help" -->` 區塊
- [x] 2.12 跑 `bash <conventions>/scripts/update-cli-help.sh` 把 `./serialwrap --help` 灌進 README marker
- [x] 2.13 `python3 -m policy_check --repo .` 全綠（R-01 ~ R-16 全 pass）

## 3. Profile schema：`bootloader_prompts`

- [x] 3.1 在 `sw_core/config.py`（或 profile parser）加 `bootloader_prompts`：接受 YAML list，只保留 str 元素；`ProfileTemplate` 與 `SessionProfile` 均以 `tuple[str, ...]`（immutable）暴露，預設 `()`
- [x] 3.2 `docs/serialwrap-spec.md` profile 章節新增欄位說明、範例 regex
- [x] 3.3 至少一個 vendor profile 補上 `bootloader_prompts`（BGW720 / Marvell；視 `profiles/` 既有檔案而定）
- [x] 3.4 單元測試：profile parser 認得 `bootloader_prompts: []`（向後相容）與含值 list

## 4. `SessionManager.self_test`：BOOTLOADER 分類

- [x] 4.1 新增 `_matches_any_bootloader_prompt(rx_tail: str, patterns: list[str]) -> str | None`（命中回 pattern、否則 None）
- [x] 4.2 在 `session_manager.py:1738` 附近 ATTACHED 區塊插入 BOOTLOADER 分支（`elif _matches_any_bootloader_prompt(...)`）
- [x] 4.3 BOOTLOADER result 加 `matched_prompt`、`rx_tail`（用 `clean_text(bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES))`）
- [x] 4.4 確認 OS prompt 與 bootloader prompt 同時匹配時優先取 BOOTLOADER（在分支順序上即 elif chain 自然成立；補 unit test）
- [x] 4.5 確認所有 classification 的 result 都帶上 `recovery_mode`（透過 `_lease_context` 同步）

## 5. `InteractiveLease` / `interactive_open` 改動（stash-and-restore）

- [x] 5.1 `sw_core/constants.py` 新增 `MAX_RECOVERY_LEASE_S = 120.0`、`BOOTLOADER_RX_TAIL_BYTES = 512`
- [x] 5.2 `InteractiveLease` dataclass 加 `recovery_mode: bool = False`、`suspended_human: bool = False`
- [x] 5.3 `SessionRuntime` dataclass 加 `_stashed_human_lease: InteractiveLease | None = None`
- [x] 5.4 `_lease_context` 加 `recovery_mode` 鍵（從 lease.recovery_mode 取）
- [x] 5.5 `interactive_open` signature 加 `allow_attached: bool = False`
- [x] 5.6 替換 `state != "READY"` 單檢，依 design §4.2 邏輯放寬：READY 走原路徑、ATTACHED + allow_attached + 當下匹配 bootloader → 開 recovery lease
- [x] 5.7 ATTACHED 路徑匹配失敗回 `SESSION_NOT_READY`，加 `error_detail: NOT_BOOTLOADER`
- [x] 5.8 recovery 開 lease 流程（design §4.3）：在 `_lock` 內檢查既有 lease；owner 為 `human:*` → stash 到 `session._stashed_human_lease`、從 `self._interactive` pop、`session.interactive_session_id=None`、`bridge.suspend_interactive()`；owner 為 agent → 回 `SESSION_INTERACTIVE_BUSY`；無既有 lease → suspended_human=False
- [x] 5.9 `_open_interactive_locked` 接 `recovery_mode` / `suspended_human` kwarg：clamp `timeout_s ≤ MAX_RECOVERY_LEASE_S`（recovery lease only）、設旗標
- [x] 5.10 `_close_interactive_locked` 處理 recovery lease：pop lease、bridge owner 設 None；若 `lease.suspended_human=True`：呼叫 `bridge.resume_interactive()`、檢查 `_stashed_human_lease.expired()` 與 `bridge.console_has_external_peer(client_id)`、皆通過則還原 stash 回 `_interactive` + `set_interactive_owner(stashed.owner)`、否則丟棄 stash；最後 `_stashed_human_lease=None`
- [x] 5.11 lease expire 路徑（`_refresh_interactive_locked` 內的 `expired()` 檢查）對 recovery lease 呼叫 `_expire_interactive_locked`，在 lock 內安全清除 `bridge._suspended_owner`，確保 stash 被正確處理（bug fix commit）
- [x] 5.12 `_refresh_interactive_locked` 行為驗證：recovery lease 期間呼叫不應誤把 stashed lease 當成失效（因為已從 `_interactive` pop）

## 6. RPC / CLI passthrough

- [x] 6.1 `sw_core/service.py` `session.interactive_open` 從 `params.get("allow_attached", False)` 讀並傳入
- [x] 6.2 `sw_core/cli.py` `session interactive-open` 加 `--allow-attached`（store_true、default False），寫入 RPC params
- [x] 6.3 CLI `--help` 文字描述用途（recovery 用、會 suspend human）
- [x] 6.4 grep `sw_mcp/`：若 MCP `interactive_open` tool 描述提到 READY-only，同步補 `allow_attached` 與 `recovery_mode` 說明

## 7. Tests（unit + functional）

- [x] 7.1 unit: `self_test classifies BOOTLOADER when bootloader_prompts matches RX tail`
- [x] 7.2 unit: `self_test falls back to ATTACHED_NOT_READY when bootloader_prompts is empty`
- [x] 7.3 unit: `self_test prefers BOOTLOADER over ATTACHED_NOT_READY when both could apply`
- [x] 7.4 unit: `interactive_open with allow_attached=False rejects ATTACHED state`（向後相容）
- [x] 7.5 unit: `interactive_open with allow_attached=True rejects ATTACHED if no bootloader match`（含 `error_detail: NOT_BOOTLOADER`）
- [x] 7.6 unit: `interactive_open with allow_attached=True opens recovery lease in BOOTLOADER`（無 human lease → suspend 不被呼叫）
- [x] 7.7 unit: `interactive_open recovery stashes human lease and restores on close`（斷言：stash 入 `_stashed_human_lease`、recovery 期間 `_refresh_interactive_locked` 不誤判失效、close 時 stash 還原 + bridge.resume_interactive flush deferred buffer）
- [x] 7.7a unit: `interactive_open recovery rejects when existing lease is agent`（owner 非 human → SESSION_INTERACTIVE_BUSY，不執行 stash）
- [x] 7.7b unit: `interactive_close recovery discards expired stash`（stash 在 recovery 期間 expire → close 時丟棄、session 回到無 lease）
- [x] 7.7c unit: `interactive_close recovery discards stash when human detached`（human 在 recovery 期間 detach → close 時 console_has_external_peer=False、stash 丟棄）
- [x] 7.8 unit: `interactive_send during recovery writes raw bytes`（plain / key encoding 各一）
- [x] 7.9 unit: `recovery lease enforces MAX_RECOVERY_LEASE_S cap`
- [x] 7.10 unit: `recovery lease auto-expires resumes human`（`_refresh_interactive_locked` 呼叫 `_expire_interactive_locked`，清 `bridge._suspended_owner`；bug fix commit 補完）
- [x] 7.11a unit: `self_test / _lease_context` 回傳 `recovery_mode` 欄位（lease 有 recovery_mode=True 時為 True、None lease 時為 False）
- [x] 7.11b unit: `interactive_status` 回傳 `recovery_mode`（待 interactive recovery 實作後）
- [ ] 7.12 func-test: fake-target 模擬 U-Boot prompt → human attach → self_test → recovery lease → reset → OS prompt → close → 驗 deferred flush
- [ ] 7.13 跑 `python3 -m pytest -q tests/` 全綠（含既有 selftest-collab-handoff scenarios 不破壞）
- [ ] 7.14 跑 `python3 -m unittest discover -s tests -v`，比對既有已知失敗（`test_multiagent_e2e.test_five_agents_three_rounds_no_conflict`）僅該案例；其餘 100% 綠

## 8. OpenSpec specs 寫入

- [ ] 8.1 `openspec/changes/2026-05-07-bootloader-recovery-44/specs/session-selftest/spec.md`：ADDED Requirements 對應 task 7.1 / 7.3 scenarios
- [ ] 8.2 `openspec/changes/2026-05-07-bootloader-recovery-44/specs/session-interactive/spec.md`：ADDED Requirements 對應 task 7.4 ~ 7.11 scenarios（`allow_attached`、`recovery_mode`、`MAX_RECOVERY_LEASE_S`、suspend-resume 行為）

## 9. Docs

- [x] 9.1 `docs/serialwrap-spec.md` self_test 章節加 BOOTLOADER classification、`bootloader_prompts` 欄位、`matched_prompt` / `rx_tail` 輸出
- [x] 9.2 `docs/serialwrap-spec.md` interactive 章節加 `allow_attached`、`recovery_mode`、`MAX_RECOVERY_LEASE_S`、suspend-resume 行為
- [ ] 9.3 `docs/serialwrap-spec.md` profile 章節加 `bootloader_prompts` 欄位、範例 regex、與 `prompt_regex` 的優先序
- [x] 9.4 `README.md`（troubleshooting / Usage 段）加 recovery 流程使用範例（self_test → interactive-open --allow-attached → interactive-send reset）

## 10. CHANGELOG / VERSION

- [x] 10.1 `CHANGELOG.md [Unreleased]` 補三條 entry（design §8）：`feat(session)` recovery、`chore(policy)` adopt conventions、`docs` OpenSpec change package
- [ ] 10.2 `VERSION` 值為 `0.0.1`（對齊既有 git tag `v0.0.1`，R-07 要求 VERSION == latest tag；release PR 才升版）

## 11. Functional verification（實機）

- [ ] 11.1 重啟 daemon、`session console-attach COM0`（人類觀察者），把板子敲進 U-Boot
- [ ] 11.2 `serialwrap session self-test --selector COM0` → 預期 `classification: BOOTLOADER`、`matched_prompt` 與 `rx_tail` 帶值
- [ ] 11.3 另一 terminal `serialwrap session interactive-open --selector COM0 --allow-attached` → 拿到 interactive_id、result 含 `recovery_mode: true`
- [ ] 11.4 `serialwrap session interactive-send --interactive-id <id> --data "printenv\n"` 觀察 RX tail 含 env list
- [ ] 11.5 `interactive-send` 送 `reset\n`、目視板子 reboot；同時 console-attach 視窗中 human 鍵盤輸入沉默（被 deferred）
- [ ] 11.6 `session interactive-close` → 驗 human deferred buffer 一次性 flush；`session self-test` 回 OK / OS state
- [ ] 11.7 反案例：故意不開 bootloader_prompts、敲進 U-Boot → self_test 回 `ATTACHED_NOT_READY`（向後相容驗證）
- [ ] 11.8 反案例：開 `--allow-attached` 但 board 已不在 U-Boot → `SESSION_NOT_READY` (`error_detail: NOT_BOOTLOADER`)

## 12. Commit / PR

- [ ] 12.1 commit 1：`chore(policy): adopt paulsha-conventions v1.0.0 (R-01 ~ R-16 baseline)`（task 2 全部）
- [ ] 12.2 commit 2：`feat(profile): add bootloader_prompts schema field`（task 3）
- [ ] 12.3 commit 3：`feat(self_test): classify BOOTLOADER when RX tail matches bootloader_prompts`（task 4）
- [ ] 12.4 commit 4：`feat(session): allow interactive_open in BOOTLOADER state with --allow-attached`（task 5 / 6）
- [ ] 12.5 commit 5：`test(session): cover bootloader recovery interactive lease`（task 7）
- [ ] 12.6 commit 6：`docs(session): document bootloader recovery + interactive_open allow_attached`（task 8 / 9 / 10）
- [ ] 12.7 push branch、開 PR title `feat(session): add bootloader recovery interactive lease (#44)`
- [ ] 12.8 PR body：套用 `.github/pull_request_template.md`、checklist 全勾、明寫 `Closes #44`
- [ ] 12.9 等 GitHub Action `Policy Check` 綠燈（R-15 dual-pin 驗證）
- [ ] 12.10 等 review、merge

## 13. Post-merge

- [ ] 13.1 `openspec` 進 archive：`mv openspec/changes/2026-05-07-bootloader-recovery-44 openspec/changes/archive/`、把 `specs/session-interactive/spec.md` 內容搬到 `openspec/specs/session-interactive/spec.md`、`specs/session-selftest/spec.md` 內容 merge 進 `openspec/specs/session-selftest/spec.md`
- [ ] 13.2 在 issue #44 留 closing comment、附 PR 連結與實機驗證紀錄
