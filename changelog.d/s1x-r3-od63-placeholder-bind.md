---
type: fix
scope: session
---

`session bind` 不再繞過 OD-63 以佔位 profile 實體化 RELEASED 佔位 session（sol 增量審查 S1-9990b18-1）：對佔位 bind 比照 `session attach` 的 RELEASED 早退，回 `ok:true`、`released:true`、`recommended_action:"device_attach"`，不解除 by_id 阻擋、不寫 binding override、不取消在途 attach、不開埠；`_attach_by_id` 另加防線，佔位 session 一律不建 bridge（attach worker 也不對佔位重試）。佔位仍只能經 `device attach` 解除後交 template 偵測。
