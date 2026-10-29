---
type: fix
scope: session
---

所屬 template 已改名或刪除的 RELEASED 紀錄不再永久阻擋裝置（S1X-a-8，擁有者裁決 OD-63）：載入時實體化為 RELEASED 佔位 session（passthrough、`command_capable:false`），by_id 維持阻擋並跨重啟保留；`device attach COMx` 時佔位移除自身並解除 by_id，已取得的 UART 交 template 偵測在同一 COM 建立正式 session（不以佔位 profile attach、不改綁其他 DETACHED 空槽）。不新增 RPC／CLI 表面；DF-04 說明同步。
