---
type: fix
scope: session
---

修正 drain 化後 `device release`／`session clear` 收尾欄位與 0.3.2 不同的回歸：release 完成時更新 `detached_at` 並重置 `reprobe_attempts`／`reprobe_exhausted`；clear 在裝置在場轉 ATTACHING 時把 `last_error` 清為 `null`，不再於 attach 完成前在 `session list` 看到 `"CLEARED"`（S1X-a-7）。
