---
type: change
scope: rust-s21
---
Slice 2.1 gate：`linux_l1` 須以非 root 執行（root 下經 `SW_GATE_L1_USER` 以 setpriv 降權，無法降權即 EVIDENCE_MISSING；judge 以 `run_euid` 判定，F108）；identity 的 `daemon_version` 改由新工具 `scripts/rust/probe_daemon_version.py` 以 throwaway daemon 的 `daemon.status` RPC 取得（F139）；`s21_gate:linux_cap_posix_pty`、`s21_gate:existing_suites_rust` 依實際收集 add-only 登記（F50／F57）；計畫 06 的 `SERVER_STOP_BUDGET` 勘誤為 20 s（F100）。
