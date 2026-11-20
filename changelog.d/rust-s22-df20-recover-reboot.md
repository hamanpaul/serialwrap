---
type: fix
scope: daemon
---
Rust daemon 依 DF-20 範圍（OD-37，rulings/p6.md）：受理的 `session recover`（READY／ATTACHED 或 `--force`）與觸發 boot quiet 的 U-Boot banner（重新開機）使既有 agent interactive lease 失效；之後以舊 `interactive_id` 呼叫 `session interactive-send` 回 `INTERACTIVE_NOT_FOUND` 且不送任何 bytes（release→重新 attach 既有行為不變）。仍掛著的 human console lease 不受影響。Python 0.4.0 現行照送，依 OD-37 不改。DF-20 登記文字同步涵蓋三種情境，新增 conditional 案例 `df20-recover-reboot-stale-interactive-id`。
