---
type: fix
scope: bridge-tool
---

修正 Windows COM peer gate 在最後寫入檢查沿用重連前 settle 起點的競態。DSR low→high 後重新起算 200 ms settle；新回歸確認過渡期間不會把 banner 寫入 COM。
