# Release 0.1.0 Repo Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 tracked `test/reports/` 測試報告、補齊 ignore 與 governance 檢查，並準備 `v0.1.0` 正式 release。

**Architecture:** 本計畫只調整 repository metadata、文件與 release artifact，不改 Python runtime 行為。清理採一般 git commit 移除目前 tree 中的 tracked report，不重寫 history；release 透過 PR 合併後再 tag 與建立 GitHub Release。

**Tech Stack:** Git、GitHub CLI、Python unittest/pytest、paulsha-conventions `policy_check`。

---

## File Structure

- Modify: `.gitignore` — 加入 `.worktrees/`、`.superpowers/`、`test/reports/` 與常見本機產物 ignore 規則。
- Delete: `test/reports/20260211-075638_serialwrapd-core_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-075728_serialwrapd-core_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-081659_session-bind_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-082229_bind-attach-fix_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-085419_multiagent-5agent_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-091629_minicom-router-alias_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-091847_minicom-router-autostart_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-092004_minicom-router-ready-select_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-092616_minicom-router-attach-install_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260211-093404_uart-flowcontrol-none_v1.1.1-serialwrapd-test.md`
- Delete: `test/reports/20260213-144649_minicom-install-autobind_v1.1.1-serialwrapd-test.md`
- Modify: `CHANGELOG.md` — 新增 `0.1.0` release 條目，保留 `[Unreleased]` 空段落。
- Modify: `VERSION` — 更新為 `0.1.0`。
- Create: `docs/releases/v0.1.0.md` — GitHub Release note 草稿。
- Inspect: `.paul-project.yml` — 確認 paulsha-conventions project config。
- Inspect: `.github/pull_request_template.md` — 確認 R-11 checklist。
- Inspect: `.github/copilot-instructions.md`、`CLAUDE.md`、`AGENTS.md`、`GEMINI.md` — 僅在 `policy_check` 指出不同步時同步修改四份。

### Task 1: 移除 tracked reports 並更新 ignore

**Files:**
- Modify: `.gitignore`
- Delete: `test/reports/*.md`

- [ ] **Step 1: 確認目前 tracked report 清單**

Run:

```bash
git ls-files 'test/reports/**'
```

Expected: 輸出 11 個 `test/reports/*.md` 檔案。

- [ ] **Step 2: 從 index 與 working tree 移除 reports**

Run:

```bash
git rm -r test/reports
```

Expected: 輸出每個 `rm 'test/reports/...md'`。

- [ ] **Step 3: 更新 `.gitignore`**

Replace `.gitignore` content with:

```gitignore
.pytest_cache/
__pycache__/
*.pyc
*.pyo

dist/
build/

# 本機 agent / worktree 目錄
.worktrees/
.superpowers/

# 本機測試與執行產物
reports/
test/reports/
htmlcov/
.coverage
*.log
*.tmp
```

- [ ] **Step 4: 驗證 report 已不再 tracked**

Run:

```bash
git ls-files 'test/reports/**'
```

Expected: 無輸出。

- [ ] **Step 5: 驗證 ignore 規則生效**

Run:

```bash
git check-ignore -v test/reports/example.md
```

Expected: 輸出包含 `.gitignore` 與 `test/reports/`。

- [ ] **Step 6: Commit 清理變更**

Run:

```bash
git add .gitignore
git commit -m "chore: 移除 tracked 測試報告" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit 包含 `.gitignore` 修改與 `test/reports` 刪除。

### Task 2: 掃描 tracked 測試產物與疑似機敏資料

**Files:**
- Inspect: all tracked files
- Inspect exception: `profiles/brcm.env`

- [ ] **Step 1: 掃描 tracked 產物路徑**

Run:

```bash
git ls-files | grep -Ei '(^|/)(test/reports|reports|coverage|htmlcov|\.pytest_cache|__pycache__|\.mypy_cache|\.ruff_cache|dist|build|\.tox|\.nox|\.eggs|.*\.(log|tmp|bak|swp|pyc|pyo|sqlite|db))(/|$|\.)' || true
```

Expected: 無輸出，或只出現經人工判定應保留的 source/test fixture；不可再出現 `test/reports/`。

- [ ] **Step 2: 掃描敏感路徑**

Run:

```bash
git ls-files | grep -Ei '(^|/)(\.env|.*\.env|.*_env|id_rsa|id_ed25519|.*\.(pem|key|p12|pfx|crt|cer|kdb|jks)|secrets?|credentials?|tokens?)(/|$|\.)' || true
```

Expected: 可接受 `profiles/brcm.env`，因使用者指定 console login profile 例外；若出現其他檔案，先檢查內容再決定移除或改成範例。

- [ ] **Step 3: 掃描敏感內容檔名**

Run:

```bash
git grep -Il -E 'BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|api[_-]?key|secret|token|password|passwd' -- . ':(exclude)profiles/**' ':(exclude).git/**' || true
```

Expected: 只出現文件、測試或程式中描述環境變數名稱與假資料的檔案；不可輸出真實 private key 或 token。不要把任何疑似 secret 值貼到 PR 或 release note。

- [ ] **Step 4: 若發現非例外真實機敏檔，停止**

Run this status command before stopping:

```bash
git --no-pager status --short
```

Expected: 若只有 false positives，繼續 Task 3；若有真實 secret，停止並回報檔名與處理建議，不揭露值。

### Task 3: 補齊 release 文件

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `VERSION`
- Create: `docs/releases/v0.1.0.md`

- [ ] **Step 1: 更新 `VERSION`**

Replace `VERSION` content with:

```text
0.1.0
```

- [ ] **Step 2: 更新 `CHANGELOG.md` release 區段**

Replace the top of `CHANGELOG.md` from `## [Unreleased]` through the current `### Notes` section with:

```markdown
## [Unreleased]

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

### Removed

- 從目前 tracked tree 移除 `test/reports/` 測試報告；不重寫既有 git history。

### Fixed

- `sw_core/session_manager.py`：動態 auto-detect session 現在會從 `ProfileTemplate` 傳播 `bootloader_prompts`，避免未宣告 targets 的 template session 無法進入 BOOTLOADER recovery

### Security

- 掃描 tracked paths 與 tracked content，確認未發現需移除的非例外機敏資料；`profiles/brcm.env` 屬 console login profile 例外。

### Notes

- Phase A 為治理/文件/CI scaffolding，不含 Issue #44 recovery 功能
- policy_check engine pinned to `ff1a031172ec24fc155699f9f3ce5bdea24d9e24`
```

- [ ] **Step 3: 建立 release note 草稿**

Create `docs/releases/v0.1.0.md` with:

```markdown
# serialwrap v0.1.0

## Highlights

- 導入 `paulsha-conventions` v1.0.0 治理基線與 policy check 工作流。
- 移除 tracked `test/reports/` 測試報告，並加入 `.gitignore` 避免再次提交本機產物。
- 完成 tracked files hygiene 掃描；`profiles/brcm.env` 為 console login profile 例外。
- 納入 bootloader recovery 相關 CLI/RPC/MCP 與測試更新。

## Validation

- `python3 -m pytest -q tests/`
- `python3 -m policy_check --repo .`

## Notes

- 本 release 不重寫 git history；`test/reports/` 僅自目前版本的 tracked tree 移除。
- GitHub Release 應於 PR merge 後從 `main` 建立 tag `v0.1.0`。
```

- [ ] **Step 4: Commit release docs**

Run:

```bash
git add CHANGELOG.md VERSION docs/releases/v0.1.0.md docs/superpowers/plans/2026-05-15-release-repo-hygiene.md
git commit -m "docs: 準備 0.1.0 release 文件" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit 包含 changelog、version、release note 與本 plan。

### Task 4: 驗證 paulsha-conventions 導入

**Files:**
- Inspect: `.paul-project.yml`
- Inspect: `.github/pull_request_template.md`
- Inspect or modify together: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md`

- [ ] **Step 1: 檢查 policy engine 是否可用**

Run:

```bash
python3 -m policy_check --repo .
```

Expected: 若 policy engine 已安裝，命令執行並回報 pass/fail；若出現 `No module named policy_check`，執行 Step 2。

- [ ] **Step 2: 必要時安裝 pinned policy engine**

Run only if Step 1 reports missing module:

```bash
python3 -m pip install --user --disable-pip-version-check "git+https://github.com/hamanpaul/paulsha-conventions.git@ff1a031172ec24fc155699f9f3ce5bdea24d9e24"
```

Expected: pip install 成功，未改動 repo tracked files。

- [ ] **Step 3: 重新執行 policy check**

Run:

```bash
python3 -m policy_check --repo .
```

Expected: PASS。若 FAIL 指出 agent 文件不同步，下一步同步四份 agent 文件；若 FAIL 指出其他具體檔案，依訊息修正後重跑。

- [ ] **Step 4: 若需同步 agent 文件，一次修改四份**

Only if Step 3 reports agent instruction mismatch, copy the same managed policy block and project policy content across:

```text
CLAUDE.md
AGENTS.md
GEMINI.md
.github/copilot-instructions.md
```

Expected: 四份檔案首行皆為 `<!-- managed-by: hamanpaul/paulsha-conventions@v1.0.0 -->`，且皆包含裸行 `policy_version: 1.0.0`。

- [ ] **Step 5: Commit policy alignment fixes if any**

Run only if Step 4 modified files:

```bash
git add CLAUDE.md AGENTS.md GEMINI.md .github/copilot-instructions.md .paul-project.yml .github/pull_request_template.md .github/workflows/policy-check.yml
git commit -m "chore: 對齊 paulsha policy 文件" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit 只包含 policy 對齊檔案。

### Task 5: 測試、PR、tag 與 GitHub Release

**Files:**
- Inspect: git diff and committed history
- Use: GitHub PR and Release

- [ ] **Step 1: 執行完整測試**

Run:

```bash
python3 -m pytest -q tests/
```

Expected: PASS，或僅出現 policy 文件已記載的既有 `tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict` agent TX count mismatch。若出現其他失敗，先修正再繼續。

- [ ] **Step 2: 執行 unittest 交叉確認**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: PASS，或與 Step 1 相同的既有失敗範圍。

- [ ] **Step 3: 執行 policy check**

Run:

```bash
python3 -m policy_check --repo .
```

Expected: PASS。

- [ ] **Step 4: 最終 hygiene 檢查**

Run:

```bash
git ls-files 'test/reports/**'
git check-ignore -v test/reports/example.md
git --no-pager status --short --branch
```

Expected: 第一個命令無輸出；第二個命令顯示 `.gitignore` 規則；第三個命令顯示目前 branch 為 `release/0.1.0-repo-hygiene`，且只有預期未追蹤的使用者目錄可留在 working tree 外。

- [ ] **Step 5: 推送分支**

Run:

```bash
git push -u origin release/0.1.0-repo-hygiene
```

Expected: remote branch 建立或更新成功。

- [ ] **Step 6: 建立 PR**

Run:

```bash
gh pr create --base main --head release/0.1.0-repo-hygiene --title "chore: 發布 0.1.0 repo hygiene release" --body "$(cat <<'EOF'
## Summary

- 移除 tracked `test/reports/` 測試報告並加入 `.gitignore`
- 確認 `paulsha-conventions` 治理基線與 policy check
- 更新 `CHANGELOG.md`、`VERSION` 與 `v0.1.0` release note 草稿

## Test Plan

- [ ] 執行 `python3 -m pytest -q tests/` — 無新增失敗
- [ ] 執行 `python3 -m unittest discover -s tests -v` — 無新增失敗
- [ ] 執行 `python3 -m policy_check --repo .` — 通過

## Policy Checklist (R-11)

- [ ] 分支不是 `main`（不可直接 commit 到 main）
- [ ] `CHANGELOG.md` 已更新（`[Unreleased]` 段落）
- [ ] `VERSION` 已更新（若有版本號變動）
- [ ] `python3 -m pytest -q tests/` 通過（無新失敗）
- [ ] `python3 -m policy_check --repo .` 通過
- [ ] 四份 agent 檔案已同步（若有修改 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md`）
- [ ] 已標記 exemption label（若適用，白名單：`policy-exempt-changelog`、`policy-exempt-tests`、`policy-exempt-version`）

## Issue Reference

- Repo hygiene and v0.1.0 release preparation
EOF
)"
```

Expected: GitHub CLI 回傳 PR URL。

- [ ] **Step 7: PR merge 後建立 tag**

Run only after PR is merged to `main`:

```bash
git fetch origin main --tags
git switch main
git pull --ff-only origin main
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

Expected: tag `v0.1.0` 推送成功。

- [ ] **Step 8: 建立 GitHub Release**

Run only after Step 7 succeeds:

```bash
gh release create v0.1.0 --title "serialwrap v0.1.0" --notes-file docs/releases/v0.1.0.md
```

Expected: GitHub Release 建立成功並回傳 URL。
