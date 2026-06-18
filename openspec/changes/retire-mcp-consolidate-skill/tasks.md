# Tasks — retire-mcp-consolidate-skill（serialwrap PR，`Closes #59`）

## 1. 退役 MCP
- [ ] 確認 `sw_core` 無 import `sw_mcp`（`grep -rn 'sw_mcp' sw_core`）
- [ ] 刪 `sw_mcp/`（含 `server.py`）與 root `serialwrap-mcp` shim
- [ ] `.paul-project.yml` code_paths 移除 `sw_mcp/**`、`serialwrap-mcp`
- [ ] `install.sh` 移除 serialwrap-mcp 安裝、`sw_mcp` 複製與 `mkdir sw_mcp`；加部署端清理（`rm -f ${TARGET_DIR}/serialwrap-mcp`、`rm -rf ${TARGET_DIR}/sw_mcp`）

## 2. 測試手術（覆蓋不淨損）
- [ ] 刪 `tests/test_mcp_completeness.py`
- [ ] `tests/test_event_mcp.py`：移除 `_TOOL_MAP`/`_TOOL_DEFS` 斷言；確認 event 經 CLI 仍有覆蓋（不足補）
- [ ] `tests/test_remote_endpoint.py`：移除 `sw_mcp.server` 路徑，改以 CLI `--endpoint` 測 remote
- [ ] `tests/test_bootloader_recovery.py:971`：移除該 MCP import/斷言
- [ ] `python3 -m pytest -q tests/` 除既有 flaky 外全過、無殘留 `sw_mcp` import

## 3. 文件去 MCP
- [ ] `README.md`：`:3` 架構描述去 `serialwrap-mcp`；移除 `:812-818`、`:1018` MCP tool/endpoint 範例（保留 CLI 等價）
- [ ] `docs/serialwrap-spec.md:561` 去 `serialwrap-mcp --endpoint` 提及

## 4. skill 整併 + 改名
- [ ] 新增 `skills/serialwrap/SKILL.md`：frontmatter `name: serialwrap`（description 去 MCP 框架）、CLI-first、合併 root skills.md + custom-skills SKILL.md、補 #51 command_capable+`PROFILE_NOT_COMMAND_CAPABLE` / #53 human_active/soft-preempt / #54 / #55
- [ ] pre-flight grep 指向 root `skills.md` 的引用，先改再刪
- [ ] 刪 root `skills.md`
- [ ] `install.sh` 加 `mkdir -p ~/.agents/skills` + `ln -sfn $SCRIPT_DIR/skills/serialwrap ~/.agents/skills/serialwrap`

## 5. 收尾驗證
- [ ] `CHANGELOG.md [Unreleased]` 記一筆（R-09）
- [ ] `python3 -m policy_check --repo .` R-01~R-22 全綠（R-22 無新懸空）
- [ ] `./install.sh` 後 `~/.agents/skills/serialwrap` 生效、`~/.paul_tools` 無 `serialwrap-mcp`/`sw_mcp`
