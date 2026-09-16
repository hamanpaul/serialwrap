---
type: docs
issue: 197
scope: remote
---
README（中英雙語）與 `SKILL.md` 的 Cloudflare 路線 B（Named Tunnel）補上 2026-09-16 真機實跑發現的兩個坑，並註明已驗證版本。純文件變更，`sw_core/` 程式碼零改動。

- **`tunnel login` 要選 zone 再按 Authorize**：只登入 Cloudflare 帳號不會產生 `~/.cloudflared/cert.pem`，cloudflared 會一直印 `Waiting for login...`。
- **`sudo cloudflared service install` 找不到設定**：sudo 之下 `~` 是 `/root`，直接執行報 `Cannot determine default configuration path`。步驟改為把 `config.yml` 與該 tunnel 的 `<UUID>.json` 搬到 `/etc/cloudflared/`、改寫 `credentials-file` 路徑，再以 `--config` 明確指定安裝，並以 journal 的 `Registered tunnel connection` 當檢查點。
- 註記路線 B 已以 cloudflared 2026.9.1 於 WSL2 Ubuntu 24.04 bench 端到端實跑（純 ssh 往返約 3 秒、`cmd submit` 取得真實 UART 輸出），對應 #197 Phase 2／Phase 3 的 Named Tunnel 部分；Quick Tunnel（Phase 1）與重開機測試仍待做，故本 PR 不關閉 #197。
