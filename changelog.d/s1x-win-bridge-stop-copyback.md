---
type: fix
scope: rust-refactor
---

`scripts/rust/win-blackbox.ps1`：`Stop-WindowsComBridge` 拋例外時（主流程 catch 與 finally 兜底）也先回寫本機 bridge 證據再刪 scratch，回寫錯誤附到 `bridge_stop`；B 輪結果另記回寫後的 `bridge_wire_output`，避免只留下指向已刪 scratch 的路徑。判定不變（例外路徑仍為 FAIL）。
