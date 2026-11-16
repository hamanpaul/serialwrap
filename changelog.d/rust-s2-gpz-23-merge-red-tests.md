---
type: fix
scope: rust-refactor
---

- 依編排者 2026-11-16 裁決修復合併 gpz 第 16～22 項後的兩支紅燈：
  - 恢復 `tests/test_rust_cfg_boundary.py` 中 `test_detects_forbidden` 的 `src-` 變體，並依裁決核准自 `p7_gate` 與 `s21_gate` 移除 `tests-` 變體。
  - 修正 `scripts/rust/s22_gate.sh` 的 `--dry-run` 行為，在列出計畫後仍產出 `s22-gate.json`。
