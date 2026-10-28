---
type: fix
scope: rust-refactor
---

Stage 1A gate 依 OD-31 把 Windows `ev-01-spawn-handler` 因 Python 基線 shell handler 在 Windows 不觸發（等待 `ev-fired` 逾時）的列、依 OD-32 把 Windows `sm-04-device-unplug` 的 `cap_missing:by_id_link` SKIP 列判為 BLOCKED-OWNER（不計 PASS）；Linux 列仍須 PASS，其他失敗型態照判 FAIL。
