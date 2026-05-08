# Proposal: ATTACHED / U-Boot fallback recovery + paulsha-conventions 導入（Issue #44）

## Why

`session.self_test` 在 `ATTACHED_NOT_READY` 狀態下建議 `console_attach` 作為 fallback，但實機驗證顯示：當 COM0 / COM1 已被 `human:*` 占用為 interactive owner 時，新 attach 進來的 console 並不會自動取得 `interactive_owner: true`，因此 PTY 寫入不會抵達 UART——`reset\n` 等 raw 指令對 U-Boot 失效。唯一可行的目前路徑是強制把人類觀察者踢掉，這與 issue #42（selftest-collab-handoff）建立的「人類觀察 + agent 控制」雙軌模型互相違背。

同時，本 repo 至今未接入 `hamanpaul/paulsha-conventions` policy engine：缺 `CHANGELOG.md` / `VERSION` / `.paul-project.yml` / `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.github/workflows/policy-check.yml` / `.github/pull_request_template.md`；既有 `.github/copilot-instructions.md` 缺 policy_version marker。本變更比過去幾個 issue 都大，正好作為 conventions 導入的第一個 PR，後續所有 PR 自動受 R-01 ~ R-16 規範。

## What Changes

### Issue #44：bootloader recovery interactive lease

- **Profile schema 新欄位** `bootloader_prompts: list[str]`（預設 `[]`）。每元素為 regex，匹配 `bridge.rx_tail` 最後一行的 bootloader prompt（U-Boot、Marvell、Broadcom CFE 等）。
- **`SessionManager.self_test`**：在 `state == "ATTACHED"` 路徑多一次 RX tail 比對；命中 → classification `BOOTLOADER`、recommended_action `recover_interactive`、result 帶 `matched_prompt` 與 `rx_tail`。OS prompt 與 bootloader prompt 同時匹配時取 `BOOTLOADER`。
- **`SessionManager.interactive_open`** 新增 `allow_attached: bool = False`：放寬 READY-only gate 至「state ∈ {ATTACHED}、bridge alive、當下重新匹配 `bootloader_prompts`」。匹配失敗回 `SESSION_NOT_READY` (`error_detail: NOT_BOOTLOADER`)。匹配成功且既有 lease 為 `human:*` 時，daemon 採 **stash-and-restore**：把 human session-layer lease 從 `self._interactive` pop 並暫存到 `session._stashed_human_lease`、呼叫 `bridge.suspend_interactive()`、開出新的 agent recovery lease；`interactive_close`（或 lease expire）時呼叫 `bridge.resume_interactive()` flush deferred buffer，並還原 stashed lease 回 `self._interactive`（若 stash 仍未 expire 且 human 仍 console-attached）。既有 lease 為 agent 時直接回 `SESSION_INTERACTIVE_BUSY`、不執行 stash。
- **`InteractiveLease`** 新增 `recovery_mode: bool`、`suspended_human: bool`；snapshot / `interactive_status` / `self_test` lease_context 透出 `recovery_mode`。
- **逾時 cap**：`allow_attached=True` 開出的 lease 強制 `timeout_s ≤ MAX_RECOVERY_LEASE_S`（120s，定義在 `sw_core/constants.py`）；超過直接 clamp 並在 result echo。
- **RPC / CLI 串接**：`session.interactive_open` 從 `params.get("allow_attached", False)` 讀；`session interactive-open` CLI 加 `--allow-attached` flag。
- **Agent 互動仍走既有 RPC**：`interactive_send`（plain / base64 / key 全部可用）/ `interactive_status` / `interactive_close` 不變、無新 verb。

### paulsha-conventions 導入（policy_profile=`flat`、policy_version=`1.0.0`）

- 新增 `.paul-project.yml`（`code_paths: ["sw_core/**", "sw_mcp/**", "tools/**", "tests/**"]` + `cli` 區塊）。
- 新增 `VERSION`（`0.0.1`，對齊既有 git tag `v0.0.1` / R-07）。
- 新增 `CHANGELOG.md`（Keep-a-Changelog 1.1.0；`[Unreleased]` 段含本 PR 條目）。
- 新增 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`（managed-by marker、policy_version、本地測試指令在地化）。
- 既有 `.github/copilot-instructions.md` 補首行 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->`、`policy_version: 1.0.0`，與其他三份 agent file 同步。
- 新增 `.github/pull_request_template.md`（含 R-11 必勾項）。
- 新增 `.github/workflows/policy-check.yml`：呼叫 `hamanpaul/paulsha-conventions` reusable workflow，雙 pin 到 `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`（main HEAD as of 2026-05-07）。
- README.md：補齊 `## Install` / `## Usage` / `## Version` 段落（若缺）。

### Change package layout

- 本 OpenSpec change 目錄：`openspec/changes/2026-05-07-bootloader-recovery-44/`，含 `proposal.md` / `design.md` / `tasks.md` / `specs/`。
- Brainstorming narrative spec：`docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`（與既有 `docs/design-*.md` 慣例一致；不違反 conventions）。

## Capabilities

### New Capabilities

- `session-interactive`：首次以 OpenSpec 形式為 `session.interactive_open` / `interactive_send` / `interactive_status` / `interactive_close` 建立 capability spec（baseline 取自既有 `docs/serialwrap-spec.md`），並引入 `allow_attached` recovery 行為與 `recovery_mode` lease 旗標。

### Modified Capabilities

- `session-selftest`：新增 `BOOTLOADER` classification 與 `bootloader_prompts` profile 欄位，補進 `openspec/specs/session-selftest/spec.md` ADDED Requirements。

## Impact

- 影響程式：
  - `sw_core/session_manager.py`（`self_test` ATTACHED 分支、`interactive_open` 入參與 gate、`InteractiveLease` 欄位、`SessionRuntime._stashed_human_lease` 欄位、`_open_interactive_locked` / `_close_interactive_locked` stash-and-restore、`MAX_RECOVERY_LEASE_S` clamp）
  - `sw_core/uart_io.py`（無預期變動；suspend/resume 既有機制重用）
  - `sw_core/service.py`（`session.interactive_open` 讀新 param）
  - `sw_core/cli.py`（`session interactive-open` 加 `--allow-attached` flag）
  - `sw_core/constants.py`（新增 `MAX_RECOVERY_LEASE_S = 120`）
  - `profiles/`（schema 文件 + 至少一個 vendor profile 補 `bootloader_prompts`）
  - `sw_mcp/`（若 MCP `interactive_open` tool 描述需同步補 `allow_attached` 與 `recovery_mode`）
- 影響測試：`tests/`（11 條新 unit scenarios）、`func-test/`（fake-target U-Boot prompt 整合案例）。
- 影響文件：
  - `docs/serialwrap-spec.md`（self_test §9.1 加 BOOTLOADER；interactive_open 章節加 allow_attached / recovery_mode）。
  - `docs/superpowers/specs/2026-05-07-issue-44-bootloader-recovery-design.md`（新檔，brainstorming narrative）。
  - `openspec/changes/2026-05-07-bootloader-recovery-44/`（本 change package）。
- 影響 repo 治理：所有後續 PR 受 R-01 ~ R-16 規範；merge 前 `python3 -m policy_check --repo .` 必須全綠。
- 不影響：UART RX path、event engine、WAL、login FSM、其他 session.* RPC、READY-state 契約。
- 相容性：`allow_attached` 為 opt-in、預設 False；`bootloader_prompts` 為 opt-in、預設 `[]`；既有 caller 行為不變。**BREAKING（行為層）**：實機 ATTACHED 狀態若 profile 宣告了 `bootloader_prompts` 且 RX tail 命中，self_test 會回 `BOOTLOADER` 而非 `ATTACHED_NOT_READY`；既有強依賴 `ATTACHED_NOT_READY` 字串的 caller 需擴展處理。
