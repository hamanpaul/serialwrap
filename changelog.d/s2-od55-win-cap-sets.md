---
type: change
scope: rust-s21
---
OD-55（Windows 部分）：`s21_core.windows` 中從不執行的 13 個 `#[ignore]` 能力列（`cap_<cap>__*`）移出。名單以原生 Windows `cargo_l1_results.py --list-only` 收集核對（全數為 ignored）。移出的列改登記於能力集合（selector `-- --ignored cap_<cap>__`）：`s21_gate:windows_cap_windows_com.windows`（8 列，沿用 gate 既有 `windows_cap_windows_com` section 的必要集合，selector 限 `--package sw-pal`）、`s21_gate:cap_windows_native.windows`（2 列）、`s21_gate:cap_interactive_desktop.windows`（2 列）、`s21_gate:cap_supervisor_runtime.windows`（1 列）。之後以 `--allow-removal` 逐名自 `s21_core.windows` 移除。各集合只收錄自 `s21_core.windows` 移出的列，原生收集中其餘未凍結的能力列不在此次登記範圍。

OD-56 擴及 windows（擁有者裁決 2026-11-12）：`s21_core.windows` 依 P7-LW-1 一對一替換，以原生收集到的
`sw-core::login_quiet::post_login_cmd_profile_logs_in`、`sw-core::session_manager::detect_skips_passthrough_and_console_unlock`
取代已刪除的 `login_quiet::unsupported_profile_no_auto_tx`（`fa847c78`）與已改名的
`session_manager::detect_skips_passthrough_console_unlock_and_unsupported`（`94526b40`）。

Slice 2.1 gate：新增 `windows_cap_{windows_native,interactive_desktop,supervisor_runtime}` 三個 windows 能力 section，
承擔 `s21_gate:cap_<cap>.windows`。證據只能於原生 Windows 本機產生（`SW_GATE_LOCAL_WINDOWS_DIR` 匯入
`<dir>/windows_cap_<cap>/l1-results.json`）；缺證據檔、平台不是 windows 或未完成時，判 EVIDENCE_MISSING（LOCAL-WINDOWS-TODO），
不得 PASS。依擁有者裁決（2026-11-12）退役 `linux_cap_windows_native`、`linux_cap_interactive_desktop` 兩個 linux section
（OD-68 後已無 linux 必要集合），改由對應的 windows section 承擔。`test_l1_lock_frozen_set_excludes_capability_gated`
比照 OD-53 豁免明確能力集合；OD-68 的「不在 lock」斷言改為「不得有 linux 平台列」。
