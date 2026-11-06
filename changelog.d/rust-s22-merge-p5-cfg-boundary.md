---
type: fix
scope: rust-tests
---
`crates/sw-core/tests/lease_selftest.rs` 讀 golden 改經 `tests/common::repo_path`，不再直接使用 `CARGO_MANIFEST_DIR`，使合併 P5 T10～T14 後 `scripts/rust/check_cfg_boundary.py` 的 `test-data-path` 規則恢復 exit 0（測試斷言不變）。
