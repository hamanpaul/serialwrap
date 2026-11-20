---
type: fix
scope: rust-s21
---

Slice 2.1 gate 的能力集合 section 改以收集器 `--caps-only` 只執行能力測試（不再先跑整個 crate 的預設測試），使 `linux_cap_sys_admin` 在具備能力（root）時可依能力列判定；`linux_cap_posix_pty` 比照 `linux_l1` 於 root 時降權為非 root 執行，無法降權即記證據不足。
