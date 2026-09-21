---
type: docs
issue: 197
scope: remote
---
README（中英雙語）新增「新 bench 佈署檢查清單」：把原本分散在 Cloudflare 實例、bench 代號、發代號與入列三節的資訊，收斂成一份可逐項打勾的操作清單——每網域一次的帳號前提、新 bench 在跑腳本前要自備的條件（含 socket 路徑確認與監管模式選擇）、三條佈署指令、controller 端驗收步驟，以及「已跑 Named Tunnel 的 bench 不可裸跑 `cloudflared tunnel --url`」的陷阱。
