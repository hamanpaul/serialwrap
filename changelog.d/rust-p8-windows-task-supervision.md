---
type: fix
scope: rust-cli
---
`windows-task` 監管下的 `serialwrap service stop`／`service restart`／routed `daemon stop` 一律經 P4 `graceful_stop`：`/Change /DISABLE` → RPC `daemon.stop` → 等待 daemon 結束（≤10 s）→ 只有等待逾時，或 `daemon.stop` 連不上且排程工作查詢仍為 Running 時才 `schtasks /End` → `/Change /ENABLE`（restart 在 `/Run` 前才重新啟用）。daemon 已正常結束時不再執行 `/End`，也不查詢工作狀態；RPC 連不上且工作已停止時不 `/End`、回報成功；RPC 連不上且工作狀態查不到時不 `/End`，以 `INTERNAL_ERROR` 回報（仍還原啟用），不再誤報健康的停止。輸出形狀不變。
