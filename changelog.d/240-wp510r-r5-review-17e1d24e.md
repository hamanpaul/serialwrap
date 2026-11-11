---
type: fix
scope: rust-core
---
依據 S2-PAR-wp510r 第 10 項第 2 輪審查（`17e1d24e`）補完 OD-63 佔位 `device.attach` 一般 OSError 收尾的裝置取消保留（P8-a07fdfb-1 殘留情境）：
- 佔位開埠登記帳本紀錄時，於同一鎖區段在預先準備的交接紀錄記下本次開埠的原路徑；之後裝置更新只要同一 by_id 不再指向原路徑（改指或離線），就寫入不可撤銷的取消，路徑改回也不清除（對齊 Python `update_devices` 對在途作業 `op.cancelled.set()`）。
- 佔位退休在 manager 鎖內消費這份取消：OSError 已結算帳本紀錄後、退休之前發生「改指再改回」時，不再誤退休佔位並交接重開原路徑，而是回 SESSION_RECOVERING、保留 RELEASED 佔位。路徑未變的同快照重掃不取消，照舊退休交接。
