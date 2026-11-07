---
type: change
scope: rust-p7
---
取消樣本在命令維持 `accepted` 並逾時時，現在回報等待階段、cmd_id、最後一次 `command.get` 回應、經過時間及 session 狀態；狀態診斷失敗會附註而不取代主要錯誤，RPC 非 ok、缺少 command 與終端態仍各自明確失敗。
