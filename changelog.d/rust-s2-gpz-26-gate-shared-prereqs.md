---
type: fix
scope: rust-gate
---

- `gate_shared.sh` 在建置與 Cargo L1 前執行共用 gate 前置檢查；失敗時記錄結果並停止後續工作。
