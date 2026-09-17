---
type: feat
issue: 203
scope: remote
---
新增 `serialwrap connect <code>` 代號連線子命令：從 `benches.yaml` 解析代號，展開成等價的
`remote -L` 參數並重用既有 spawn 路徑建立隧道，成功後把 loopback endpoint 記入
`benches.state.json`；`--close` 拆除該代號的隧道並清除 endpoint 記憶。

- 未知代號、參數錯誤一律走既有 `TunnelError` → CLI JSON `{ok:false,error_code}` 邊界，例外不穿越 CLI。
- 建立失敗時回滾已開的隧道；拆除前驗證隧道歸屬，避免誤關他人或孤兒隧道。
- endpoint state 更新序列化，避免併發寫入互相覆蓋。
