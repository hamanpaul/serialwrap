---
type: fix
scope: sw-pal
---

修正 fake ProcScan 對重拉 daemon 回報的 socket：Unix RPC URL 轉回原始 `--socket` 路徑，TCP endpoint 維持原值。這讓 Windows 原生 legacy handoff 測試與真實行程掃描的欄位語意一致。
