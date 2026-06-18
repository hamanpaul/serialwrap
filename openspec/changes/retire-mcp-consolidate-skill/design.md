## Context

serialwrap 的 MCP 層（`sw_mcp/server.py` + root `serialwrap-mcp` shim）是 skill 機制出現前的產物，現已 vestigial：非合規 MCP、三 host 未註冊、零腳本呼叫、`_TOOL_MAP` 與 CLI 1:1 重複（完整評估見 `docs/superpowers/specs/2026-06-18-serialwrap-retire-mcp-consolidate-skill-design.md` §1–2，及上游 `2026-06-18-serialwrap-docs-align-and-skill-consolidation-design.md` §2.4/§2.5）。本 change 退役它並把 skill 整併進 repo、改名去除誤導性的 `-mcp`。**不改任何 capability 行為**——故無 spec delta（`specs/` 為空）。

## Goals / Non-Goals

**Goals**
- 硬刪 MCP adapter（`sw_mcp/`、`serialwrap-mcp` shim），含 code_paths / install.sh / 文件 / 測試清理。
- skill 整併為 repo 內唯一權威來源 `skills/serialwrap/SKILL.md`（CLI-first、改名 `serialwrap`），`install.sh` symlink 到 `~/.agents/skills/`。

**Non-Goals**
- 合規 MCP-over-HTTP（雲端 agent 用，未來才做）；`.mcp.json` / plugin / marketplace / slash command（#59 原提，捨棄）。
- 不動 daemon / CLI / RPC 介面與行為（device-handoff / mcu-flash-broker / session-* capability 不變）。
- custom-skills 移除舊份 = 另一 repo 的獨立 PR，不在本 change。

## Decisions

- **硬刪而非 deprecation stub**：零 caller、未註冊 → YAGNI。
- **測試覆蓋不淨損**：刪 `test_mcp_completeness.py`；其餘 3 個 MCP-coupled 測試移除 adapter 斷言時，確認 event / remote-endpoint / bootloader 功能在 CLI 路徑仍有覆蓋，不足則補 CLI 測試。
- **skill 落點 `skills/serialwrap/SKILL.md`**：`install.sh` 定向 symlink（非掃描全 root），對齊 custom-skills 的 `~/.agents/skills` 慣例。

## Risks

- 移除 MCP 路徑測試若連帶失去功能覆蓋 → 以 CLI 等價測試補。
- 移除 root `skills.md` / 改 README 產生懸空引用 → R-22 把關 + pre-flight grep。

## Migration

無資料/介面遷移；CLI、daemon、RPC 不變。既有以 `serialwrap-mcp --tool` 呼叫者：無（已確認零腳本依賴）。agent 改用 `serialwrap <group> <subcmd>` CLI（skill 已 CLI-first）。
