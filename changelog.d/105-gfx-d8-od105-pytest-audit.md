---
type: fix
scope: rust-s2-gfx-d8
---
依 OD-105 逐名自兩個 Linux 全套 pytest L1 必要集合移出 139 個 skipped 與 1 個 xfailed nodeid；不刪除測試或改變 selector。S24 Windows COM 節點改由其凍結必要 case `s24-ho-windows-com` 精確承擔，不混入 Linux required set。四個 gate 僅以 `VERSION` 推導的 `release:<version>` label 執行 `policy_check`，並將實際 label 留存在 gate 記錄中；同時修正審查文件中的本機絕對路徑。
