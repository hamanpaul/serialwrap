---
type: change
scope: rust-s21
---
OD-55（Windows 部分）：`s21_core.windows` 中從不執行的 13 個 `#[ignore]` 能力列（`cap_<cap>__*`）移出。名單以原生 Windows `cargo_l1_results.py --list-only` 收集核對（全數為 ignored）。移出的列改登記於能力集合（selector `-- --ignored cap_<cap>__`）：`s21_gate:windows_cap_windows_com.windows`（8 列，沿用 gate 既有 `windows_cap_windows_com` section 的必要集合，selector 限 `--package sw-pal`）、`s21_gate:cap_windows_native.windows`（2 列）、`s21_gate:cap_interactive_desktop.windows`（2 列）、`s21_gate:cap_supervisor_runtime.windows`（1 列）。之後以 `--allow-removal` 逐名自 `s21_core.windows` 移除。各集合只收錄自 `s21_core.windows` 移出的列，原生收集中其餘未凍結的能力列不在此次登記範圍。

OD-56 擴及 windows（擁有者裁決 2026-11-12）：`s21_core.windows` 依 P7-LW-1 一對一替換，以原生收集到的
`sw-core::login_quiet::post_login_cmd_profile_logs_in`、`sw-core::session_manager::detect_skips_passthrough_and_console_unlock`
取代已刪除的 `login_quiet::unsupported_profile_no_auto_tx`（`fa847c78`）與已改名的
`session_manager::detect_skips_passthrough_console_unlock_and_unsupported`（`94526b40`）。
