---
type: change
scope: rust-p7
---
L1 必要集合鎖新增 `p7_gate:cargo-test-workspace`（linux，selector `cargo test --workspace --features test-fakes`），以 `regen_lock.py --l1-add-only` 登記 parity #49 `cargo:sw-core::login_quiet::console_unlock_arm_mid_login_finish_applied`；既有集合不變（sol 補審 #4）。
