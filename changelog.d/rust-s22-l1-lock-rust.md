---
type: fix
scope: rust-tests
---
P6 新增的 Rust 測試以 `regen_lock.py --l1-add-only` 登記進 L1 凍結分母：`s22_gate:cargo_workspace`（linux，141 筆）與 `s22_gate:console_conformance_pty`（linux，7 筆 `cap_posix_pty__*`）；新增 `tests/test_s22_l1_lock_rust.py` 靜態掃描 P6 Rust 測試檔，未登記即紅。Windows 集合待原生收集（LOCAL-WINDOWS-TODO，sol 補審 P6 #1）。
