---
type: fix
scope: rust-p7
---
`sw_core::recover::RecoverPlan::AwaitDrain` 改為 `DrainBusy`：drain 進行中的 session recover 依 Python 立即回 `SESSION_RECOVERING`＋drain 欄位，不等待 StopOp 回條後 reattach（parity-amendments-2026-10-26 #57）。
