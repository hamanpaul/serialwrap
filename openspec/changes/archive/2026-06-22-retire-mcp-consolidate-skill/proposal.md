## Why

serialwrap 的「MCP」層是空殼：`sw_mcp/server.py` 非合規 MCP（自訂 `{tool,params}` JSON 行，無 `initialize`/`tools/list`/`tools/call`）、三個 agent host 都沒註冊、零腳本呼叫、`_TOOL_MAP` 與 CLI 1:1 重複。它是 skill 機制出現前的產物，現已被 skill + CLI 完全取代（評估見設計文件 §2.4/§2.5）。同時 skill 兩份（repo `skills.md` 與 `hamanpaul/custom-skills/serialwrap-mcp`）雙向漂移。本 change 退役 MCP、把 skill 整併進 serialwrap repo 並改名，消除誤導性的 `-mcp` 命名與三處重複維護的漂移源。

## What Changes

- **退役 MCP**：硬刪 `sw_mcp/server.py`（連同 `sw_mcp/`）與 repo 根 `serialwrap-mcp` shim；`.paul-project.yml` code_paths 移除 `sw_mcp/**`、`serialwrap-mcp`；`install.sh` 停止安裝 shim / 複製 sw_mcp；移除 README / `docs/serialwrap-spec.md` 的 MCP tool 範例。
- **測試手術**：刪 `tests/test_mcp_completeness.py`；`tests/test_event_mcp.py`、`tests/test_remote_endpoint.py`、`tests/test_bootloader_recovery.py` 移除 MCP-adapter 斷言，並確保 event / remote-endpoint / bootloader 功能在 CLI 路徑仍有覆蓋（不足則補 CLI 測試）。
- **skill 整併 + 改名**：root `skills.md` → `skills/serialwrap/SKILL.md`（權威單一來源，CLI-first，加 frontmatter `name: serialwrap`，補 #51/#53 缺漏並合併 #54/#55 handoff 內容）；`install.sh` symlink `skills/serialwrap` → `~/.agents/skills/serialwrap`。
- **不在本 change**：`hamanpaul/custom-skills` 移除舊 `serialwrap-mcp/`（另一 repo 的獨立 PR）。

## Capabilities

### New Capabilities
<!-- 無：不引入新 capability。 -->

### Modified Capabilities
<!-- 無：不變更任何 capability 的 requirement 行為。僅移除 MCP adapter（非 openspec capability）並整併 docs/skill；daemon / CLI / RPC（device-handoff / mcu-flash-broker / session-* 等 capability）介面與行為皆不動。 -->

## Impact

- **刪除**：`sw_mcp/`（含 `server.py`）、root `serialwrap-mcp` shim、`tests/test_mcp_completeness.py`、root `skills.md`。
- **修改**：`.paul-project.yml`（code_paths）、`install.sh`（移除 MCP、新增 skill symlink）、`tests/{test_event_mcp,test_remote_endpoint,test_bootloader_recovery}.py`、`README.md`、`docs/serialwrap-spec.md`、`CHANGELOG.md`。
- **新增**：`skills/serialwrap/SKILL.md`。
- **gate**：觸及 code_paths → R-09（CHANGELOG）必觸發；R-22 對移除 `skills.md` / 改 README 不可產生新懸空引用。
- **無 capability 行為變更**；安裝後 `~/.paul_tools` 不再有 `serialwrap-mcp`、`sw_mcp/`。
