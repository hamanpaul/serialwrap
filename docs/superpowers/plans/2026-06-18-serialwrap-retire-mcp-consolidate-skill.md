# #59 退役 MCP + skill 整併/改名 實作計畫（serialwrap PR）

> **給 agentic worker：** 必用子技能——`superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans` 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**目標：** 退役 serialwrap 的 vestigial MCP 層、把 agent skill 整併為 repo 內唯一權威來源並改名 `serialwrap-mcp` → `serialwrap`，且不改任何 capability 行為。

**架構：** 單一 serialwrap PR（`Closes #59`）。先做退役 MCP（含真實測試手術，以 pytest 當 RED→GREEN harness），再做文件去 MCP 與 skill 整併（以 `policy_check` + grep + skill-load 當 harness）。custom-skills 移除舊份為另一 repo 的獨立 PR、不在本計畫。

**技術棧：** pytest、`paulsha-conventions` policy engine（`python3 -m policy_check`）、git、bash（install.sh）、markdown（SKILL.md frontmatter）。

**設計來源：** `docs/superpowers/specs/2026-06-18-serialwrap-retire-mcp-consolidate-skill-design.md`；openspec change `openspec/changes/retire-mcp-consolidate-skill/`。

**常數：** 已在分支 `feature/59-retire-mcp-consolidate-skill`（off main）。

---

## Task 1：確認 sw_mcp 無被 production code 依賴（前置）

**Files:** 無（探測）

- [ ] **Step 1：grep sw_core/CLI 是否 import sw_mcp**

Run:
```bash
grep -rnE 'import sw_mcp|from sw_mcp|sw_mcp\.' sw_core/ serialwrap serialwrapd.py 2>/dev/null; echo "EXIT=$?"
```
Expected：無輸出（`EXIT=1`）。MCP 是 adapter，production code 不該依賴它。若有輸出 → 停下評估（設計假設被推翻）。

## Task 2：RED — 刪 sw_mcp，證明 MCP-coupled 測試確實耦合

**Files:**
- Delete: `sw_mcp/`（含 `server.py`）、root `serialwrap-mcp`

- [ ] **Step 1：刪除 MCP adapter**

Run:
```bash
git rm -r sw_mcp serialwrap-mcp
```

- [ ] **Step 2：跑 pytest，確認 RED（正確理由＝模組已刪）**

Run:
```bash
python3 -m pytest -q tests/ 2>&1 | tail -20
```
Expected：`tests/test_mcp_completeness.py`、`tests/test_event_mcp.py`、`tests/test_remote_endpoint.py`、`tests/test_bootloader_recovery.py` 出現 **ImportError / collection error**（`No module named 'sw_mcp'`）。這證明這 4 檔對 MCP adapter 的耦合。**先不修，記錄 RED 輸出。**

## Task 3：GREEN — 測試手術（覆蓋不淨損）

**Files:**
- Delete: `tests/test_mcp_completeness.py`
- Modify: `tests/test_event_mcp.py`、`tests/test_remote_endpoint.py`、`tests/test_bootloader_recovery.py`

- [ ] **Step 1：刪整檔 `test_mcp_completeness.py`**（純測 `_TOOL_MAP`，隨 MCP 退役失去意義）

Run:
```bash
git rm tests/test_mcp_completeness.py
```

- [ ] **Step 2：`test_event_mcp.py` 移除 MCP 斷言、改測 CLI/RPC event 路徑**

讀檔確認它測什麼（event rule 載入/狀態/tail）。移除 `from sw_mcp.server import _TOOL_DEFS, _TOOL_MAP` 及對 `_TOOL_MAP` 的斷言。若 event 功能僅由此檔的 MCP 路徑覆蓋，改以 CLI（`serialwrap event add/status/tail`）或直接 RPC（`sw_core` service）等價測試保留覆蓋；若 event 已在他處（如 `test_event_*.py`）覆蓋，則本檔可整檔刪。在報告說明採哪種。

- [ ] **Step 3：`test_remote_endpoint.py` 改以 CLI 測 remote**

`:197-201` 的 `from sw_mcp.server import main` + `patch("sw_mcp.server.call_tool")` 改為以 `sw_core.cli` 的 `--endpoint` 路徑測 remote（同一 `rpc_call`）。確保 remote-endpoint 功能仍有斷言。

- [ ] **Step 4：`test_bootloader_recovery.py:971` 移除 MCP import/斷言**

讀該處上下文，移除 `from sw_mcp import server as mcp_server` 與相關斷言；該測試的 bootloader 行為其餘部分保留。

- [ ] **Step 5：跑 pytest，確認 GREEN**

Run:
```bash
python3 -m pytest -q tests/ 2>&1 | tail -15
grep -rn 'sw_mcp' tests/ ; echo "leftover sw_mcp refs EXIT=$?"
```
Expected：除既有 flaky（`test_five_agents_three_rounds_no_conflict`、`t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`）外無失敗；`grep sw_mcp tests/` 無輸出（`EXIT=1`）。

## Task 4：清 config / install.sh 的 MCP

**Files:**
- Modify: `.paul-project.yml`、`install.sh`

- [ ] **Step 1：`.paul-project.yml` code_paths 移除 MCP 兩行**

移除 `  - "sw_mcp/**"` 與 `  - "serialwrap-mcp"`（保留其餘 code_paths）。

- [ ] **Step 2：`install.sh` 移除 MCP 安裝/複製，加部署端清理**

移除 `:15` `mkdir -p "${TARGET_DIR}/sw_mcp"`、`:22` `install ... serialwrap-mcp`、`:29` `cp -a "${SCRIPT_DIR}/sw_mcp/." ...`。在「Remove legacy artifacts」段加：
```bash
rm -f "${TARGET_DIR}/serialwrap-mcp"
rm -rf "${TARGET_DIR}/sw_mcp"
```

- [ ] **Step 3：bash 語法檢查**

Run:
```bash
bash -n install.sh && echo "install.sh syntax OK"
```

## Task 5：文件去 MCP

**Files:**
- Modify: `README.md`、`docs/serialwrap-spec.md`

- [ ] **Step 1：`README.md`**

`:3` 架構描述把 `serialwrap-mcp` 移除（改述由 `serialwrapd` + `serialwrap` CLI + skill 組成）。移除 `:812-818` 的 `serialwrap-mcp --tool ...` 範例區塊與 `:1018` 的 `serialwrap-mcp --endpoint` 範例；若該段落是「MCP 用法」整節，整節刪除或改寫為 CLI 等價。

- [ ] **Step 2：`docs/serialwrap-spec.md:561`**

移除 `- \`serialwrap-mcp\` 與 CLI 相同，也支援全域 \`--endpoint\`` 這行（或改為只講 CLI `--endpoint`）。

- [ ] **Step 3：grep 確認 repo 已無 MCP shim 引用**

Run:
```bash
grep -rnE 'serialwrap-mcp|serialwrap_[a-z]+ ' README.md docs/serialwrap-spec.md skills.md 2>/dev/null | grep -v 'docs/superpowers' | head
```
Expected：除 skills.md（Task 6 處理）外，README/spec 不再有 `serialwrap-mcp` 與 `serialwrap_*` MCP tool 引用。

## Task 6：skill 整併進 repo + 改名 + symlink

**Files:**
- Create: `skills/serialwrap/SKILL.md`
- Delete: root `skills.md`
- Modify: `install.sh`

- [ ] **Step 1：pre-flight — grep 指向 root `skills.md` 的引用**

Run:
```bash
grep -rnE 'skills\.md' README.md docs/ 2>/dev/null | grep -v 'docs/superpowers'; echo "EXIT=$?"
```
若有引用，先把它們改指向 `skills/serialwrap/SKILL.md`（或移除），避免 R-22 懸空。

- [ ] **Step 2：建立 `skills/serialwrap/SKILL.md`（權威單一來源，CLI-first）**

frontmatter：
```yaml
---
name: serialwrap
description: <去掉「MCP」框架；聚焦 serialwrap broker + CLI 多 agent UART 單寫者仲裁、RAW logging、device handoff、MCU flash>
---
```
內容：以 root `skills.md` 為骨幹（remote/event/file/參數/安全規則/短命令原則），**改寫為 CLI-first**——移除 ~30 個 `serialwrap_*` tool 對應清單與所有 `serialwrap-mcp --tool` 範例，改以 `serialwrap <group> <subcmd>`（如 `serialwrap cmd submit`、`serialwrap device release/attach`、`serialwrap session self-test`）。合併 `custom-skills/serialwrap-mcp/SKILL.md` 的 device handoff(#54) 與 MCU(#55) 段；補 #51 command_capable + `PROFILE_NOT_COMMAND_CAPABLE`、#53 human_active/soft-preempt（語意對齊 `openspec/specs/session-command-readiness`、`session-interactive`）。

- [ ] **Step 3：刪 root `skills.md`**

Run:
```bash
git rm skills.md
```

- [ ] **Step 4：`install.sh` 加 skill symlink**

在安裝段加：
```bash
mkdir -p "${HOME}/.agents/skills"
ln -sfn "${SCRIPT_DIR}/skills/serialwrap" "${HOME}/.agents/skills/serialwrap"
```
並 `bash -n install.sh` 檢查語法。

- [ ] **Step 5：frontmatter / skill 結構驗證**

Run:
```bash
head -4 skills/serialwrap/SKILL.md   # 確認 frontmatter name/description 合法
grep -cE 'serialwrap_[a-z]+|serialwrap-mcp --tool' skills/serialwrap/SKILL.md  # 期望 0（已 CLI-first）
```

## Task 7：收尾驗證 + CHANGELOG + commit

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1：`CHANGELOG.md [Unreleased]` 記一筆**

```markdown
- 退役 vestigial MCP 層（刪 `sw_mcp/` + `serialwrap-mcp` shim、清 code_paths/install.sh/README/docs/測試）；agent skill 整併為 repo 內唯一權威來源 `skills/serialwrap/SKILL.md`（CLI-first，改名 `serialwrap-mcp`→`serialwrap`），`install.sh` symlink 到 `~/.agents/skills/`。不改 capability 行為。（#59）
```

- [ ] **Step 2：完整 policy_check**

Run:
```bash
python3 -m policy_check --repo .; echo "EXIT=$?"
```
Expected：`EXIT=0`，R-01~R-22 無 FAIL（R-09 因 code_paths 變動需 CHANGELOG entry — 已加；R-22 無新懸空）。

- [ ] **Step 3：install + skill-load 煙測**

Run:
```bash
./install.sh >/dev/null 2>&1
ls -l ~/.agents/skills/serialwrap && ls ~/.paul_tools/serialwrap-mcp 2>&1 | head -1
```
Expected：`~/.agents/skills/serialwrap` symlink 存在；`~/.paul_tools/serialwrap-mcp` 不存在（No such file）。

- [ ] **Step 4：commit**

Run:
```bash
git add -A
git commit -m "feat(skill): 退役 MCP + skill 整併進 repo 並改名 serialwrap-mcp→serialwrap（#59）

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 驗收對照（self-review）

- `sw_mcp/` 與 `serialwrap-mcp` shim 已刪；`grep sw_mcp` 在 `sw_core`/`tests` 無殘留。
- 4 個 MCP-coupled 測試處理完畢且 event/remote/bootloader 功能覆蓋不淨損；pytest 除既有 flaky 外全過。
- `.paul-project.yml`/`install.sh`/README/`serialwrap-spec.md` 無 `serialwrap-mcp`/`serialwrap_*` 殘留。
- `skills/serialwrap/SKILL.md` 為 CLI-first 權威來源、frontmatter `name: serialwrap`、含 #51/#53/#54/#55；root `skills.md` 已刪、無懸空引用。
- `install.sh` symlink 到 `~/.agents/skills/serialwrap`；安裝後 `~/.paul_tools` 無 MCP 殘留。
- `policy_check` EXIT=0；`CHANGELOG` 已記（R-09）。
- 範疇：custom-skills 移除舊份為另一 repo 的獨立 PR，不在本計畫。
