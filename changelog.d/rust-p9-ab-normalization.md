---
type: fix
scope: stage3
---
A/B 正規化只忽略成功 `daemon status`（RPC `health.status`）回應最上層的 `foreign_holders` `/proc` 掃描欄位；其他子命令、錯誤回應與巢狀同名欄位仍嚴格比較。PID 佔位標籤改由正規化路徑分群建立，同群內取各排列的最小渲染結果，避免 PTY 鍵碰撞合併不同 PID 時標籤依走訪順序變動，並忽略 PID 清單順序。
