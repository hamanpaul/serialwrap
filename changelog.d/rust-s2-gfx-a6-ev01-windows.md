---
type: fix
scope: rust-s2-gfx
---

P7 gate 納入 Windows Stage 2 Rust daemon 的 `ev-01-spawn-handler` 原生結果；缺少證據、候選 HEAD／bundle SHA-256 不符或結果未達 `PASS`＋`gate_eligible` 一律不通過，缺證據標為 `INCOMPLETE`。bundle 身分檢查亦拒絕含禁用測試鉤子的執行檔。`win-func-test.sh` 支援 Stage 2 bundle 與只跑一次指定案例。
