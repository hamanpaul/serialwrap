---
type: fix
issue: 59
scope: setup
---
`serialwrap setup` 物化新版 `~/.agents/skills/serialwrap` skill 後，會清理舊版 `~/.agents/skills/serialwrap-mcp` legacy symlink，避免 agent 同時載入新舊 skill 並誤走已退役 MCP 流程；清理僅針對已知 legacy symlink，不刪除同名真實目錄或一般檔。
