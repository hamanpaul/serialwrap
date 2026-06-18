# 退役 MCP + skill 整併進 repo + 改名 設計（#59 reframed）

- 日期：2026-06-18
- 對應 issue：[#59](https://github.com/hamanpaul/serialwrap/issues/59)（原「打包成 Claude Code plugin」提案，本設計**重畫方向**為退役 MCP）
- 前置：[#67](https://github.com/hamanpaul/serialwrap/issues/67) 已完成（PR #68，文件對齊 + 升 conventions 1.0.5）。
- 狀態：設計（brainstorming 產出），待 user 審閱 → writing-plans。
- 上游決策來源：`docs/superpowers/specs/2026-06-18-serialwrap-docs-align-and-skill-consolidation-design.md` §2.4（MCP 退役評估）、§2.5（remote/NAT 與 MCP 正交）。

## 1. 背景與方向重畫

#59 原提案是「把 serialwrap 打包成 **含 MCP server** 的 Claude Code plugin（`.mcp.json` 註冊、補上『MCP tool 未註冊』的洞）」，且 #59 標明僅評估、實作另開 change。

#67 brainstorm 期間查明 serialwrap 的「MCP」是空殼（`sw_mcp/server.py` 非合規 MCP、三 host 都沒註冊、零腳本呼叫、`_TOOL_MAP` 與 CLI 1:1 重複），判定**退役**。這與 #59 原提案（修好並 bundle MCP）**相反**。經 user 確認：

- 沿用 #59、在 issue 留 comment 更新方向；實作 PR `Closes #59`。
- 方向改為：**退役 MCP + skill 整併進 serialwrap repo + 改名 `serialwrap-mcp` → `serialwrap` + `install.sh` symlink 到 `~/.agents/skills/` + 移除 custom-skills 那份**。
- 不做：`.mcp.json` / `.claude-plugin` plugin、marketplace 分發、commands/ slash command（#59 原提的，全部捨棄）。

## 2. 現況事實（實作依據）

### 2.1 MCP 退役的測試耦合
`grep sw_mcp tests/` → 4 檔 10 處：
- `tests/test_mcp_completeness.py`：整檔測 `_TOOL_MAP` 完整性（對照 `sw_core.service.SerialwrapService.rpc`）→ **整檔刪**。
- `tests/test_event_mcp.py:4,24,25`：`from sw_mcp.server import _TOOL_DEFS, _TOOL_MAP`，斷言 event tool 在 map 內 → 移除 MCP 斷言。
- `tests/test_remote_endpoint.py:197,201`：`from sw_mcp.server import main` + `patch("sw_mcp.server.call_tool")` 測 remote → 改以 CLI 路徑測 remote endpoint。
- `tests/test_bootloader_recovery.py:971`：`from sw_mcp import server as mcp_server` 一處 → 移除該 MCP 斷言。
- **硬性要求**：移除上述 MCP-adapter 斷言時，確認 event / remote-endpoint / bootloader 三項**功能本身**在 CLI 路徑仍有測試覆蓋；若某項僅由 MCP 路徑覆蓋，**補等價 CLI 測試**，不得淨損失覆蓋。

### 2.2 `serialwrap-mcp` / MCP 在 repo 的引用面
- `.paul-project.yml:13`：code_paths 含 `serialwrap-mcp`（另有 `sw_mcp/**`）。
- `install.sh:22`：安裝 `serialwrap-mcp` shim；`:29`：`cp -a sw_mcp/.`。
- `skills.md`：整檔即舊 skill 文件（含大量 `serialwrap-mcp --tool` 與 `serialwrap_*` 範例）。
- `README.md:3`（架構描述列 `serialwrap-mcp`）、`:812-818`、`:1018`（MCP tool / endpoint 範例）。
- `docs/serialwrap-spec.md:561`（`serialwrap-mcp --endpoint` 提及）。

### 2.3 skill 安裝與 symlink 現況
- 外部 skill 在 `hamanpaul/custom-skills/serialwrap-mcp/SKILL.md`；其 `install.sh` 以 `DEST=$HOME/.agents/skills` 掃 `REPO_ROOT/*` 內含 `SKILL.md` 的目錄做 symlink。
- 現有 `~/.agents/skills/serialwrap-mcp -> custom-skills/serialwrap-mcp`（custom-skills install 只新增、不清 stale）。
- serialwrap 自己的 `install.sh` 目前**不**做任何 skill symlink。

## 3. 設計 A：範疇與 PR 結構

- **serialwrap PR（`Closes #59`）**：退役 MCP（§4）+ skill 整併/改名（§5），單一 PR。
- **custom-skills PR（另一 repo）**：移除 `serialwrap-mcp/` + 清 stale symlink（§6）。
- **#59 issue**：先 comment 更新方向。
- **順序**：serialwrap PR 先 merge + `install.sh` 跑過（`~/.agents/skills/serialwrap` 就位）→ 再做 custom-skills 移除，避免 skill 空窗。

## 4. 設計 B：退役 MCP（serialwrap PR）

1. **硬刪**（零 caller、未註冊、YAGNI，不留 deprecation stub）：`sw_mcp/server.py`（連同 `sw_mcp/` 目錄）、repo 根的 `serialwrap-mcp` shim。
2. `.paul-project.yml`：code_paths 移除 `sw_mcp/**` 與 `serialwrap-mcp`。
3. `install.sh`：移除 `:22` serialwrap-mcp 安裝、`:29` sw_mcp 複製、`:15` `mkdir sw_mcp`；移除部署端殘留（選擇性 `rm -f ${TARGET_DIR}/serialwrap-mcp`、`rm -rf ${TARGET_DIR}/sw_mcp`）。
4. **測試**：刪 `test_mcp_completeness.py`；改 `test_event_mcp.py` / `test_remote_endpoint.py` / `test_bootloader_recovery.py` 移除 MCP-adapter 斷言，依 §2.1 硬性要求確保功能覆蓋不淨損。
5. README（`:3`、`:812-818`、`:1018`）、`docs/serialwrap-spec.md:561` 移除 MCP/`serialwrap-mcp`，保留 CLI 等價說明。
6. `CHANGELOG.md [Unreleased]`（觸及 code_paths → R-09 必記）。

## 5. 設計 C：skill 整併 + 改名 + install.sh symlink（serialwrap PR）

1. 新增 `skills/serialwrap/SKILL.md`，作為**權威單一來源**：
   - frontmatter：`name: serialwrap`、`description` 去掉「MCP」框架（聚焦 broker + CLI 多 agent UART）。
   - **CLI-first**：移除 ~30 個 `serialwrap_*` MCP tool 清單與 `serialwrap-mcp --tool` 範例，改以 `serialwrap <group> <subcmd>` 表達。
   - 內容合併：取 root `skills.md`（remote/event/file/參數較全）+ custom-skills SKILL.md（device handoff #54 / MCU #55 較新）兩者；補 #51 command_capable + `PROFILE_NOT_COMMAND_CAPABLE`、#53 human_active/soft-preempt。
2. `install.sh` 新增：`mkdir -p "${HOME}/.agents/skills"` + `ln -sfn "${SCRIPT_DIR}/skills/serialwrap" "${HOME}/.agents/skills/serialwrap"`。
3. 移除 repo 根 `skills.md`（內容已搬至 `skills/serialwrap/SKILL.md`）；更新任何指向 `skills.md` 的引用（R-22 把關，pre-flight grep）。
4. `README.md:3` 架構描述去掉 `serialwrap-mcp`（改述 `serialwrap` CLI + skill）。

## 6. 設計 D：custom-skills PR（跨 repo）

- 移除 `custom-skills/serialwrap-mcp/`（含其 `SKILL.md`）。
- 清 stale symlink：`rm -f ~/.agents/skills/serialwrap-mcp`（custom-skills install 只新增不清舊）。
- 在 serialwrap PR 落地、`install.sh` 跑過之後執行（先有 `~/.agents/skills/serialwrap` 再移除舊的）。

## 7. 驗證

- `python3 -m pytest -q tests/`：重整後測試全過、無殘留 `sw_mcp` import（`grep -rn 'sw_mcp\|serialwrap-mcp' sw_core tests` 應僅剩無關項）；除既有 flaky 外無新失敗。
- `python3 -m policy_check --repo .`：R-01~R-22 全綠；R-09（code_paths 變動有 CHANGELOG）；R-22（移除 skills.md / 改 README 不產生新懸空引用）。
- skill 可探索：`~/.agents/skills/serialwrap` symlink 生效、`SKILL.md` frontmatter 合法（claude/codex 載得到 `serialwrap` skill）。
- 安裝後 `~/.paul_tools` 不再有 `serialwrap-mcp`、`sw_mcp/`。

## 8. Non-goals

- 雲端 / 瀏覽器 agent 用的**合規 MCP-over-HTTP** server（remote/NAT 與 MCP 正交，見上游設計 §2.5）——未來有需求再做。
- `.mcp.json` / `.claude-plugin/plugin.json` plugin 形式、marketplace 分發、`commands/` slash command（#59 原提，捨棄）。
- 不改任何 serialwrap **capability 行為**；daemon / CLI / RPC 介面不動（只移除 MCP adapter 這層）。

## 9. Pre-flight / 待辦

- [ ] 確認 event / remote-endpoint / bootloader 功能在 CLI 路徑既有測試覆蓋（決定要不要補 CLI 測試）。
- [ ] grep 全 repo 指向 root `skills.md` 的引用（移除前先改）。
- [ ] 確認 `sw_core` 無 import `sw_mcp`（應無，MCP 是 adapter；若有則需處理）。

## 10. 已知既有 flaky（非本次，列此免誤判）

- `tests/test_multiagent_e2e.py::...::test_five_agents_three_rounds_no_conflict`（CLAUDE.md 載明）
- `t8_full_run_simulation`、`test_t1_wal_reset_preserves_console`（機率性，pre-existing）
