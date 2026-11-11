---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 10 項審查（`a07fdfb8`）修正 OD-63 佔位 `device.attach` 一般 OSError 收尾遺失裝置取消的問題（P8-a07fdfb-1）：
- 佔位開埠回 PermissionError 等一般 OSError 時，開埠工作在結算帳本紀錄的同一鎖區段讀出已寫入的取消（例如開埠期間同一 by_id 改指新路徑）並回報佔位 actor；已取消即回 SESSION_RECOVERING、丟棄預先準備的交接 worker，不退休、不解除 RELEASED，也不交接新路徑（對齊 Python `attach_device` 的已取消 OSError 分支）。
- 無 handle 的佔位退休改在 manager 鎖內核對裝置仍指向本次開埠的原路徑；OSError 結算之後、退休之前發生的改指視同已取消，不再誤退休佔位並開啟新路徑。
