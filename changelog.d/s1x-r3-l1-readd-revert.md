---
type: fix
scope: rust-refactor
---

撤回 `68d0941` 以 `--l1-add-only` 夾帶回 `s1b_cargo` 的 10 筆已由 OD-16／OD-17 核准移除的凍結列（cap_* 擁有者機台列、`windows_switch_running_exe_rename`、`real_mkbundle_dev_output_verifies`），經 `regen_lock.py --allow-removal` 逐名撤回，修復 `test_l1_lock_frozen_set_excludes_capability_gated`。
