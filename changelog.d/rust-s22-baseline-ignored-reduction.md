---
type: fix
scope: rust-tests
---
`scripts/rust/compare_test_baseline.py` 將基線非 `ignored`（如 `FAILED`）而待測變成 `ignored` 的測試視為縮減（`REDUCED`，exit 1），不再當成「狀態變化」放行；核准的平台排除須以 `--allow-ignored '<binary>::<測試名>'` 逐名記錄（sol 補審 P6 #5）。
