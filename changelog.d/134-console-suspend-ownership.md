---
type: fix
issue: 134
scope: windows
---
修復 Windows TCP console 於 agent 命令執行期間（suspend）連入時被誤授 raw interactive ownership 的問題（自 #84 PORT-2 起存在）：`suspend_interactive()` 會把 `_interactive_owner` 暫存為 None，新連入的 console 因此被誤判為「首個 console」直接取得 raw ownership——鍵擊繞過 deferred buffer 直寫 UART、汙染 agent 命令輸出，且 `resume_interactive()` 以暫存值蓋回後該 console 永久卡在 line-buffer 模式。修正授予條件為 suspend-aware：suspend 中且原無 owner 時改記到 `_suspended_owner`，期間輸入走既有 deferred 分支累積、resume 時無縫接手 raw ownership 並 flush；suspend 前已有 owner 者維持第二 console 的 line-buffer 行為。POSIX（PTY／lease 路徑）不經此程式碼、行為不變。
