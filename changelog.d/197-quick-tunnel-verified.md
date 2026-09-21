---
type: docs
issue: 197
scope: remote
---
回填 Cloudflare 路線 A（Quick Tunnel）的真機實跑結果與一個新坑：bench 若已跑 Named Tunnel，裸的 `cloudflared tunnel --url ssh://localhost:22` 會沿用 `/etc/cloudflared/config.yml` 的 `tunnel:`／`credentials-file:`，起出來的是**該 Named Tunnel 的第二個 connector** 而非 Quick Tunnel——仍會印出 trycloudflare hostname，但該 hostname 一律回 `websocket: bad handshake`，且 production hostname 會被 edge 分流到這個多出來的 replica。必須以 `--config <空檔>` 隔離，並檢查啟動的 `Settings: map[...]` 不含 cred-file。同步標註路線 A 已驗證版本與延遲數據（README 中英兩段＋SKILL.md）。
