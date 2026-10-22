# G 待擁有者裁決清單（S1X 組）

由 `owner-decisions-addenda.md` 分出（該檔僅保留表格列，`od09_freeze._append_addenda` 於 EOF 追加）。以下皆為待裁決項目，未裁決前不得視為已核准。（審查 L-5：下列標註「已由 OD-16 裁決」之項目已於 2026-10-22 裁決，本清單保留為歷史對照，現行狀態以 owner-decisions-addenda.md 為準。）

- OD-S1X-01（Linux）（已由 OD-16 裁決，見 :210 起）：是否同意自 `s1a_cargo`／`s1b_cargo` 移除 ignored 案例（移除者須留在 BLOCKED-OWNER 總表）。逐名清單見下節「G1-l1-lock-vs-selector」之「OD-S1X-01（Linux）」。
- OD-S1X-01（Windows）（已由 OD-16 裁決，見 :210 起）：Windows 集合的 add-only 登記須以真機收集輸出完成（WSL 無法產生）。另見下節「G1-l1-lock-vs-selector」之「OD-S1X-01（Windows）」（placeholder 12 筆、cfg 不可編譯 58 筆、posix_only 委派列、cap 列之移除裁決，以及 add-only 登記替換測試）。
- OD-S1X-02（已由 OD-16 裁決，見 :210 起）：Windows golden 採 (i) 依 plan 04 Task 10 的 12 情境並縮減分母（實作目前擷取 14 情境，含 2 boundary），或 (ii) 改 plan 擷取鎖定的全部 test_bb_cli_help 節點（432 筆）。
- OD-S1X-03（已由 OD-16 裁決，見 :210 起）：docker Rust-CLI L1.5 是否屬 Stage 1 範圍；目前一律 BLOCKED-PREREQ，非 PASS。
- EDR 資料（spike3）：win-wal-av-* 與 win-wal-f12-progress 的 `--edr-product`／`--edr-status`，由擁有者 EDR 主控台查得；未知即 BLOCKED-OWNER，不得 PASS。
- BLOCKED-PREREQ-S1X-04（已由 OD-16 裁決，見 :210 起）：systemd 實跑的基底 glibc（debian:12-slim／2.36 vs 二進位 GLIBC_2.39）；三個候選見 addenda 列，須擇一裁決後才能 pin 新映像。
- Windows cli_help oracle（`tests/contract_blackbox/test_bb_cli_help.py::test_platform_oracle_exists_and_boundary_values_are_native` 與 windows golden）：屬新 oracle，須非實作者單審通過後才可凍結。

## G1-l1-lock-vs-selector（2026-10-22 補登；OD-16 (1)(2) 已裁決並套用，見上列「OD-16 套用紀錄」）

**本節性質**：逐名列出 `tests/acceptance/l1_required.lock.json` 中與所屬列 selector 不一致的 nodeid，供擁有者裁決。本輪只補登文件：未修改鎖檔、未移除任何節點、未取消任何 `#[ignore]`、未改 `l1_judge.py` 規則或 gate 會計。相關列維持 INSUFFICIENT／FAIL，並列入 PR。

**權威與依據**：lock selector 為權威（plan 04 §5b、plan 05 Task 16 步驟 3、plan 03 Task 13：集合皆為非 `cap_*` 測試；不適用的案例不得列入該平台集合）。核對方式：

- cargo 名單：以 `crates/` 原始碼的 `#[cfg]`／`#[ignore]`／模組宣告靜態推導。Windows 目標求值時 `sw-pal` 的 `test-fakes` 由 `sw-cli` 的 dev-dependency 統一。推導出的 Windows 缺席數為 12＋58＝70，與 gate 回報的 missing 70 相符；若未統一 feature 則應為 72，故此推導支持統一成立。Linux 側：鎖內節點除舊名兩筆外皆存在於 HEAD 原始碼；`s1b_cargo.linux` 未登記 `mkbundle_respects_cargo_target_dir_override`（該測試僅登記於 `s1a_cargo.linux`，屬 add-only 範圍，非本次不一致）。
- pytest 名單：以 Linux 上 `--collect-only -m "core_portable or windows_native"` 核對（markexpr 與平台無關，conftest 的 Windows 收集排除另計）。
- Windows 原生執行報告已存在（2026-10-22，`target/win-test-report/`）：`l1-results-261022-103627-4017409.json`（s1a，864 筆，sha256 `351692fa…f5241`）與 `l1-results-261022-110431-4122629.json`（s1b，737 筆，sha256 `91995a4f…a3e`）。推導之 Windows 缺席數 70 與 provisional gate 之 `s1a_cargo.windows` missing 70 逐名一致（審查者核對）。**Windows 收集檔（鎖內名單對應之原生 `--list-only` 輸出）仍未產生**，故 Windows 各項之最終核准前仍須以原生收集覆核（見下「裁決後的執行程序」步驟 4、5）。

### OD-S1X-01（Linux）：`s1a_cargo.linux`、`s1b_cargo.linux`

| # | nodeid | 所在集合 | 性質 | 不適用原因（已核對） | 對應替換測試／證據 | 狀態 |
|---|---|---|---|---|---|---|
| L-1 | `cargo:sw-cli::mgmt_layout::mkbundle_respects_cargo_target_dir_override` | `s1a_cargo.linux` | `#[ignore = "由 tests/test_rust_mkbundle.py 驅動…"]`；CHG-S1X-L1-01（afc1ec0）add-only 登記 | selector `--workspace --all-features` 不執行 ignored；judge 判 `skipped_required`（`skip_kind=other`）。非 `cap_` 前綴，亦非 owner_gated，不轉 BLOCKED-OWNER，單獨使本列 INSUFFICIENT | pytest `tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override`（內含 `--ignored --exact`，斷言 `1 passed`） | 未移除，待裁決 |
| L-2 | `cargo:sw-cli::mgmt_layout::real_mkbundle_dev_output_verifies` | `s1a_cargo.linux`、`s1b_cargo.linux` | `#[ignore = "由 tests/test_rust_release_manifest_schema.py 驅動…"]` | 同 L-1 | pytest `tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields`（內含 `--ignored --exact`，斷言 `1 passed`） | 未移除，待裁決 |
| L-3 | `cargo:sw-cli::mgmt_service::service_non_string_mode_date_is_typeerror_exit1_without_stdout` | `s1a_cargo.linux` | 舊名；原始碼已無此函式（c62f606 改名），恆缺席（`missing`） | `cargo:sw-cli::mgmt_service::service_non_string_mode_date_reports_internal_error_json`（已登記於 `s1a_cargo.linux`） | 併入 `PENDING-OD-S1X-L1-REMOVAL`（已記於 addenda） | 未移除，待裁決 |
| L-4 | `cargo:sw-cli::mgmt_service::service_non_string_mode_mixed_keys_is_typeerror_exit1_without_stdout` | `s1a_cargo.linux` | 同 L-3 | `cargo:sw-cli::mgmt_service::service_non_string_mode_mixed_keys_reports_internal_error_json`（已登記） | 同 L-3 | 未移除，待裁決 |
| L-5 | 擁有者機台列（同類規則，非 (a)–(e) 所列）：`cargo:sw-daemon::launcher::cap_windows_native__launcher_gui_subsystem`、`cargo:sw-daemon::launcher::cap_windows_native__launcher_parent_kill_kills_child`、`cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c`（Linux 變體為 `cfg(not(windows))`，執行即 panic）、`cargo:sw-pal::service_ctl::cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure`、`cargo:sw-cli::mgmt_layout::windows_switch_running_exe_rename`（OWNER_GATED，非 `cap_` 前綴） | `s1a_cargo.linux`、`s1b_cargo.linux`（各 5 筆） | `#[ignore]` | 擁有者機台／權限相依（plan 04 §3 B3、B10；plan 05 附錄 B B-02；`scripts/rust/owner_gated.py`） | 無（BLOCKED-OWNER） | 依 plan 04 5c(i)，僅當 INSUFFICIENT 只由此類列造成時轉 BLOCKED-OWNER；與 L-1～L-4 並存時整體仍 INSUFFICIENT。是否比照 Windows 自 Linux 集合移出（`test_l1_judge` 只規範 Windows）由擁有者裁決 |

### OD-S1X-01（Windows）：`s1a_cargo.windows`、`s1b_cargo.windows`、`s1a_blackbox.windows`、`s1a_delegation.windows`

**W-1　placeholder 12 筆（`s1a_cargo.windows`）**

1e24d68 的核准只涵蓋 Linux：該提交的 diff 只在 `s1a_cargo.linux`（12 筆）與 `s1b_cargo.linux`（12 筆占位＋2 筆舊名）移除這些節點，commit message 亦寫「對齊 Linux S1A/S1B 必要集合」；`s1a_cargo.windows` 從未移除。本輪未找到 Windows 移除的獨立核准紀錄，故**不得直接引用 1e24d68**，須一併送審。檔案 `crates/sw-cli/tests/p4_case_placeholders.rs` 已於 1e24d68 刪除，此 12 節點在任何平台都不存在，Windows 集合恆缺席。

| 逐名節點（`cargo:sw-cli::p4_case_placeholders::…`） | p4 case（platforms） | 替換測試（`s1b_cargo.windows` 已登記的 cargo 筆數；另有 pytest 替換） | 判定意見 |
|---|---|---|---|
| `df05_daemon_locate_order` | df05-daemon-locate-order（linux, windows） | `mgmt_daemon::start_locate_order_sibling_then_runtime` 等（3） | 適用 Windows；移除須核准 |
| `df07_bad_config_yaml` | df07-bad-config-yaml（linux, windows） | `mgmt_service::supervision_mode_bad_yaml_is_one_stderr_line_exit1` 等（4） | 適用 Windows；移除須核准 |
| `df10_system_scope_state_path` | df10-system-scope-state-path（**linux**） | `mgmt_setup::df10_system_state_migration_target`（1） | Windows 不適用（p4 map 明定 linux；plan 03 Task 13 不得列入） |
| `df12_setup_ran_field` | df12-setup-ran-field（linux, windows） | `mgmt_setup::on_demand_to_systemd_user_stops_waits_then_starts`、`mgmt_setup::windows_default_target_is_windows_task`（2） | 適用 Windows；移除須核准 |
| `df13_minicom_broker_rename` | df13-minicom-broker-rename（**linux**） | `mgmt_setup::materialize_wrappers_posix_only`（1） | Windows 不適用（同 df10） |
| `df16_windows_skill_copy` | df16-windows-skill-copy（**windows**） | `mgmt_setup::materialize_skill_always_refreshed_and_linked`、`fs_lock::symlink_or_copy_dir_windows_fallback_copies`（2） | Windows 專屬；替換已存在，移除須核准 |
| `sc12_reconcile_wait_old_daemon` | sc12-reconcile-wait-old-daemon（linux, windows） | `mgmt_setup::on_demand_to_systemd_user_stops_waits_then_starts`、`mgmt_setup::reconcile_aborts_when_old_daemon_never_exits`（2）；pytest `test_setup_ab_reconcile_timeout` | 適用 Windows；移除須核准 |
| `sc18_windows_ondemand_autostart` | sc18-windows-ondemand-autostart（windows） | `mgmt_daemon::autostart_*` 等（16）；pytest `test_autostart_l15_tx_once` | 同上 |
| `sc20_daemon_restart` | sc20-daemon-restart（linux, windows） | `mgmt_daemon::restart_*` 等（7）；pytest `test_daemon_restart_ab` | 同上 |
| `sc22_supervision_mode_set` | sc22-supervision-mode-set（linux, windows） | `mgmt_setup`、`mgmt_supervision_switch` 之 `supervision_set_*` 等（25） | 同上 |
| `sc23_setup_rollback` | sc23-setup-rollback（linux, windows） | `mgmt_update::*` 等（87）；pytest `test_fresh_install_blackbox` | 同上 |
| `sc25_release_manifest_version_pairing` | sc25-release-manifest-version-pairing（linux, windows） | `mgmt_doctor::version_pairing_*` 等（7）；pytest `test_doctor_ab_shared_checks`、`test_doctor_ab_version_pairing_in_bundle` | 同上 |

**計數說明（修正輪補記）**：上表「替換筆數」為近似值，不得直接用於核准。審查者以腳本式核對得 `mgmt_update` 相關 92 筆（本表寫 87）、`version_pairing_*` 6 筆（本表寫 7）；核准前須由腳本重算並附指令與輸出。

12 筆逐名（供核准清單使用）：

```
cargo:sw-cli::p4_case_placeholders::df05_daemon_locate_order
cargo:sw-cli::p4_case_placeholders::df07_bad_config_yaml
cargo:sw-cli::p4_case_placeholders::df10_system_scope_state_path
cargo:sw-cli::p4_case_placeholders::df12_setup_ran_field
cargo:sw-cli::p4_case_placeholders::df13_minicom_broker_rename
cargo:sw-cli::p4_case_placeholders::df16_windows_skill_copy
cargo:sw-cli::p4_case_placeholders::sc12_reconcile_wait_old_daemon
cargo:sw-cli::p4_case_placeholders::sc18_windows_ondemand_autostart
cargo:sw-cli::p4_case_placeholders::sc20_daemon_restart
cargo:sw-cli::p4_case_placeholders::sc22_supervision_mode_set
cargo:sw-cli::p4_case_placeholders::sc23_setup_rollback
cargo:sw-cli::p4_case_placeholders::sc25_release_manifest_version_pairing
```

**W-2　cfg 於 Windows 目標不編譯（58 筆，`s1a_cargo.windows`）**

這些測試位於 `#[cfg(unix)]`（57 筆，其中 7 筆經 `#[cfg(unix)] pub mod posix;` 進入）或 `#[cfg(target_os = "linux")]`（1 筆：`process_ctl::spawn_daemon_closes_inherited_non_cloexec_fds_in_both_modes`）之下；Windows 目標不編譯，原生收集必然缺席。與 W-1 的 12 筆合計 70，即 gate 回報的 missing 70。

| 檔案（全部 `sw-pal`） | 筆數 | cfg | 同一檔案內的 Windows 原生候選（名稱含 `windows`，待確認，非等價證明） |
|---|---|---|---|
| `src/fs_ops/posix.rs` | 1 | `cfg(unix)`（`pub mod posix`） | 0 |
| `src/rpc_transport/posix.rs` | 3 | `cfg(unix)`（`pub mod posix`） | 0 |
| `src/sysenv/posix.rs` | 3 | `cfg(unix)`（`pub mod posix`） | 0 |
| `tests/fs_lock.rs` | 4 | `cfg(unix)`；`cfg(all(feature = "test-fakes", unix))` | 4 |
| `tests/fs_read.rs` | 1 | `cfg(unix)` | 0 |
| `tests/osstr.rs` | 3 | `cfg(unix)` | 0 |
| `tests/process_ctl.rs` | 5 | `cfg(unix)`；1 筆為 `cfg(target_os = "linux")` | 2 |
| `tests/process_tunnel.rs` | 8 | `cfg(unix)` | 4 |
| `tests/proc_scan.rs` | 9 | `cfg(unix)` | 10 |
| `tests/registry_view.rs` | 1 | `cfg(unix)` | 1 |
| `tests/rpc_client_conformance.rs` | 4 | `cfg(unix)` | 0 |
| `tests/sysenv.rs` | 2 | `cfg(unix)` | 0 |
| `tests/sysenv_mgmt.rs` | 14 | `cfg(unix)`；1 筆為 `cfg(all(unix, feature = "test-fakes"))` | 3 |

無同檔 Windows 候選者計 17 筆（`fs_ops`／`rpc_transport`／`sysenv` 的 posix 模組 7 筆、`fs_read` 1、`osstr` 3、`rpc_client_conformance` 4、`sysenv.rs` 2）。這些是 POSIX 專用行為，移除前須擁有者裁決：(i) 補 Windows 原生替換測試（add-only，需 Windows 原生收集）後移除，或 (ii) 經裁決免替換移除（§11.3）。有同檔候選者，候選名稱須由測試作者確認等價後才可作為替換依據。

逐名（58 筆，供核准清單使用）：

```
cargo:sw-pal::fs_lock::atomic_write_uses_expected_modes_and_removes_failed_temp_files
cargo:sw-pal::fs_lock::copy_preserving_and_copy_tree_accept_readonly_files
cargo:sw-pal::fs_lock::copy_tree_follows_source_file_symlinks_and_rejects_existing_destination
cargo:sw-pal::fs_lock::realfs_copy_tree_matches_native_destination_and_symlink_contract
cargo:sw-pal::fs_read::exists_follows_symlinks_like_os_path_exists
cargo:sw-pal::osstr::invalid_bytes_become_surrogateescape_code_points
cargo:sw-pal::osstr::join_restores_original_bytes_exactly
cargo:sw-pal::osstr::valid_utf8_is_single_text_piece
cargo:sw-pal::proc_scan::identity_alive_detects_pid_reuse
cargo:sw-pal::proc_scan::incomplete_proc_stat_is_conservatively_alive
cargo:sw-pal::proc_scan::missing_proc_stat_is_not_alive
cargo:sw-pal::proc_scan::pid_alive_self_true_and_reaped_false
cargo:sw-pal::proc_scan::pid_and_identity_alive_handle_non_utf8_comm_and_matching_ticks
cargo:sw-pal::proc_scan::posix_tty_holder_scan_reports_matching_pid_and_ok_status
cargo:sw-pal::proc_scan::posix_tty_holder_scan_reports_permission_status
cargo:sw-pal::proc_scan::posix_tty_holder_scan_reports_unknown_on_unreadable_fd_tree
cargo:sw-pal::proc_scan::unreadable_proc_stat_is_conservatively_alive
cargo:sw-pal::process_ctl::call_foreground_returns_negative_signal_status
cargo:sw-pal::process_ctl::poll_treats_echild_as_exited_when_sigchld_is_ignored
cargo:sw-pal::process_ctl::spawn_daemon_closes_inherited_non_cloexec_fds_in_both_modes
cargo:sw-pal::process_ctl::spawn_daemon_detached_new_session
cargo:sw-pal::process_ctl::spawn_daemon_poll_preserves_signal_exit_status
cargo:sw-pal::process_tunnel::posix_alive_is_false_after_child_exits
cargo:sw-pal::process_tunnel::posix_alive_is_true_for_a_running_child
cargo:sw-pal::process_tunnel::posix_rejects_invalid_pid_and_process_group_values
cargo:sw-pal::process_tunnel::posix_spawn_detached_does_not_inherit_non_cloexec_parent_fds
cargo:sw-pal::process_tunnel::posix_spawn_detached_reports_missing_and_non_executable_programs
cargo:sw-pal::process_tunnel::posix_terminate_escalates_and_waits_for_term_ignoring_group
cargo:sw-pal::process_tunnel::posix_terminate_kills_whole_group_including_grandchild
cargo:sw-pal::process_tunnel::spawn_detached_no_pipes_stdout_to_log
cargo:sw-pal::registry_view::registry_view_posix_unsupported
cargo:sw-pal::rpc_client_conformance::unix_enoent_matches_python_message
cargo:sw-pal::rpc_client_conformance::unix_fixture_rows_match_python
cargo:sw-pal::rpc_client_conformance::unix_len_107_vs_108
cargo:sw-pal::rpc_client_conformance::unix_path_too_long_has_no_errno
cargo:sw-pal::sw_pal::fs_ops::posix::tests::atomic_write_temp_file_is_close_on_exec
cargo:sw-pal::sw_pal::rpc_transport::posix::review_regression_tests::clamped_poll_timeout_does_not_finish_a_later_deadline
cargo:sw-pal::sw_pal::rpc_transport::posix::review_regression_tests::expired_send_deadline_returns_timeout
cargo:sw-pal::sw_pal::rpc_transport::posix::review_regression_tests::only_einprogress_is_treated_as_pending_connect
cargo:sw-pal::sw_pal::sysenv::posix::tests::merged_runner_preserves_stdout_and_stderr_write_order
cargo:sw-pal::sw_pal::sysenv::posix::tests::retry_buffer_grows_after_erange
cargo:sw-pal::sw_pal::sysenv::posix::tests::retry_buffer_stops_at_one_megabyte
cargo:sw-pal::sysenv::realpath_resolves_symlinks
cargo:sw-pal::sysenv::vars_survive_non_utf8_values
cargo:sw-pal::sysenv_mgmt::has_systemd_false_when_dir_absent
cargo:sw-pal::sysenv_mgmt::run_invalid_utf8_error_spans_match_python_goldens
cargo:sw-pal::sysenv_mgmt::run_invalid_utf8_is_decode_error_like_python
cargo:sw-pal::sysenv_mgmt::run_raw_preserves_nul_bytes_and_crlf
cargo:sw-pal::sysenv_mgmt::run_raw_replaces_env
cargo:sw-pal::sysenv_mgmt::run_timeout_covers_pipe_drain_after_parent_exit
cargo:sw-pal::sysenv_mgmt::run_timeout_covers_pipe_drain_after_parent_is_killed
cargo:sw-pal::sysenv_mgmt::run_universal_newlines_matches_python_golden
cargo:sw-pal::sysenv_mgmt::user_identity_posix_prefers_user_env
cargo:sw-pal::sysenv_mgmt::which_all_dedups_in_path_order
cargo:sw-pal::sysenv_mgmt::which_all_keeps_distinct_candidate_strings
cargo:sw-pal::sysenv_mgmt::which_all_skips_non_executable_posix
cargo:sw-pal::sysenv_mgmt::which_treats_empty_path_entries_as_current_directory
cargo:sw-pal::sysenv_mgmt::which_uses_default_path_when_path_is_unset
```

**W-3　`@pytest.mark.posix_only` 委派列（`s1a_blackbox.windows`、`s1a_delegation.windows` 各 1 筆）**

- nodeid：`tests/contract_blackbox/test_bb_delegation.py::test_exec_replaces_process_and_sigterm_reaches_child`
- 不適用原因：標記為 `posix_only`＋`rust_only`，Windows selector `-m "core_portable or windows_native"` 必定 deselect，judge 判 `deselected_required`。核對：以 Windows markexpr 於 Linux 做 `--collect-only`，`s1a_blackbox.windows` 的 589 筆中僅此一筆未被收集；`s1a_delegation.windows` 的 18 筆中僅此一筆未被收集。（`s1b_20groups.windows` 的 3 筆皆在收集內。）
- 替換測試：同檔 `tests/contract_blackbox/test_bb_delegation.py::test_windows_exec_or_wait_exit_code_and_terminate_propagation`（`@pytest.mark.windows_native`，第 317 行）。**目前未登記於任何鎖**：須於 Windows 原生執行 `pytest --collect-only` 後，對 `s1a_blackbox.windows` 與 `s1a_delegation.windows` 各做 add-only 登記；新增不需核准，移除舊 nodeid 需核准。該替換測試之證據須於 Windows 原生實跑（B 輪），不得以 Linux 收集代替。
- **原生實跑狀態（修正輪補記）**：2026-10-22 Windows 原生 B 輪（`target/win-blackbox/261022-112423-4157452`）此測試 **FAIL**（父行程 TerminateProcess 後委派子行程仍輸出 JSON）。登記見 addenda `PENDING-OD-S1X-WIN-DELEGATE-TERMINATE`（BLOCKED-REVIEW）。未修正並原生轉綠前，不得作為 W-3 移除舊測試的依據。

**W-4　cap 列（`s1b_cargo.windows` 2 筆）**

| nodeid | 標記 | 不適用原因 | 替換 | 狀態 |
|---|---|---|---|---|
| `cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c` | `#[ignore = "cap:interactive_desktop"]`（`#[cfg(windows)]` 變體） | 需桌面互動 console（plan 04 §3 B3） | 無（BLOCKED-OWNER） | 移出須核准；核准後仍列 BLOCKED-OWNER |
| `cargo:sw-pal::service_ctl::cap_supervisor_runtime__windows_task_roundtrip` | `#[ignore = "cap:supervisor_runtime"]` | 需擁有者 Windows 主機；會註冊並刪除測試排程工作（plan 05 B-02，需擁有者同意） | 無（BLOCKED-OWNER） | 同上 |

此 2 筆使 `tests/test_l1_judge.py::test_l1_lock_frozen_set_excludes_capability_gated` 紅燈（本輪基線：1 failed、21 passed）。

**W-5　同 Windows 集合之其他擁有者機台列（非 (e) 所列，同類規則）**

- `cargo:sw-cli::mgmt_layout::windows_switch_running_exe_rename`（`s1b_cargo.windows`；OWNER_GATED，非 `cap_` 前綴，故該測試不攔）：`#[ignore = "BLOCKED-OWNER: …"]`，擁有者 host 原生執行。替換：無。是否比照 W-4 移出由擁有者裁決；若不移出，依 5c(i) 僅在 INSUFFICIENT 只由此類列造成時轉 BLOCKED-OWNER。
- `cargo:sw-cli::mgmt_layout::real_mkbundle_dev_output_verifies`（`s1b_cargo.windows`）：同 L-2，替換為 `tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields`。

**W-6　需 Windows 原生覆核的假設（非移除項）**

- `cargo:sw-pal::registry_view::read_bt_ports_reads_fake_registry_values_and_swallows_errors`、`cargo:sw-pal::review_regressions::scripted_delay_advances_fake_clock_then_returns_reply`（`s1a_cargo.windows`）屬 `cfg(feature = "test-fakes")`。推導假設 Windows 收集經 `sw-cli` 的 dev-dependency 統一該 feature；原生收集若未統一，兩筆亦會成為 missing，須一併列入裁決。

### 裁決後的執行程序（本輪未執行；未核准前不得執行）

1. 擁有者逐項核准上列各表；未核准項目維持原狀，不得部分套用。
2. 收集輸入：`scripts/rust/cargo_l1_results.py --list-only` 依設計保留 ignored 與 `cap_*`（見該檔註解）。移除必須由收集檔**明示排除**核准名單，不得由工具自動排除，排除清單須寫入 commit 訊息。
2a. **替換前置（修正輪補記，須先於步驟 3）**：L-1、L-2 的替換 pytest（`tests/test_rust_mkbundle.py::test_mkbundle_respects_cargo_target_dir_override`、`tests/test_rust_release_manifest_schema.py::test_manifest_schema_accepts_new_optional_fields`）目前未被任何凍結集合或不變量點名，僅隱含於 pytest-full。須先以 `--l1-add-only` 將兩個替換 nodeid 登記為必要集合（或加入 stage1a coverage 不變量），之後才可 `--allow-removal` 移除舊名。W-1 至 W-5 的替換測試同此處理。
3. Linux（`s1a`：`-- --workspace --all-features`；`s1b`：`-- --workspace`）：`python3 scripts/rust/cargo_l1_results.py --platform linux --list-only --out <收集檔> -- …` → 排除核准項 → `python3 tests/acceptance/regen_lock.py --l1-add-only --set <s1a_cargo|s1b_cargo> --platform linux --collect <收集檔> --allow-removal <逐名>`（集合已存在，不需 selector）。
4. Windows cargo 收集**必須於 Windows 原生執行**（WSL 以 `--platform windows` 做的是交叉編譯，不得代替）：`s1a` 為 `-- --package sw-proto --package sw-pal --package sw-cli`，`s1b` 為 `-- --package sw-pal --package sw-cli`。排除核准項後執行 `regen_lock.py --l1-add-only --set <s1a_cargo|s1b_cargo> --platform windows --collect <收集檔> --allow-removal <逐名>`。`--allow-removal` 須與實際消失集合完全相符，工具會拒絕未列名或未消失的項目。
5. Windows pytest（原生）：`py -3.12 -m pytest --collect-only -q tests/contract_blackbox -m "core_portable or windows_native"`（`s1a_delegation` 收集範圍限 `test_bb_delegation.py`）→ `regen_lock.py --l1-add-only --set <s1a_blackbox|s1a_delegation> --platform windows --collect <收集檔> --allow-removal tests/contract_blackbox/test_bb_delegation.py::test_exec_replaces_process_and_sigterm_reaches_child`。新增替換測試於同一次收集中出現，即為 add-only 登記。
6. 驗證：`python3 tests/acceptance/regen_lock.py --check`；`python3 tests/acceptance/regen_lock.py --check --base HEAD --allow-removal <全部核准名>`；`python3 -m pytest -q tests/test_l1_judge.py`（預期 `test_l1_lock_frozen_set_excludes_capability_gated` 轉綠）。CI 的 `--allow-removal` 清單同步屬 `PENDING-OD-S1X-L1-REMOVAL` 未提交部分，須另案處理。

### 本輪狀態（2026-10-22）

- 鎖檔未修改（與 HEAD 相同）；未移除任何 nodeid；未取消任何 `#[ignore]`；未改 judge 或 gate。
- `regen_lock.py --check` 與 `--check --base HEAD` 皆 rc=0；`tests/test_l1_judge.py` 為 1 failed（`test_l1_lock_frozen_set_excludes_capability_gated`）、21 passed。
- 受影響列維持 INSUFFICIENT／FAIL：`s1a_cargo.linux`、`s1b_cargo.linux`、`s1a_cargo.windows`、`s1b_cargo.windows`、`s1a_blackbox.windows`、`s1a_delegation.windows`。
- 未執行 Windows 原生收集（鎖未改，無需收集）；Windows 原生實跑於修正輪另行進行，結果見 addenda 與 `docs/rust-refactor/reviews/pending-sol-review.md` 所列 commit；相關 Windows 結論仍待覆核。
- 擁有者機台列（cap 與 `windows_switch_running_exe_rename`）依 plan 04 §3 B3、B10 與 plan 05 B-02 仍為 BLOCKED-OWNER，不因本節而改變狀態。

## 修正輪補記（2026-10-22，Reviewer A 發現之處置）

- **BLOCKED-PREREQ-S1X-04 耦合**：`scripts/rust/register_p4_evidence.py::_l1_outcome` 以整個 `s1b_20groups` 集合的 judge verdict 為準。任一列 failed（含 BLOCKED-PREREQ 的 `test_update_systemd_exit1_not_relaunched_l15`）都會使所有 pytest 型 P4 案例判 FAIL。本輪不改 judge、不縮 selector；待 BLOCKED-PREREQ-S1X-04 解決後重算。
- **20groups 佈局衝突**：plan 05 Task 16 Step 4 以 `target/dev-bundle-sentinel` 跑 `s1b_20groups.linux`，該佈局無 `bin/serialwrapd` 與 `sw_core`，三案失敗（審查者觀察，本輪未重跑）。改用完整 `mkbundle.sh --dev` 會移除 `--delegate-sentinel` 的非委派守門（`SW_DELEGATE_MARKER`），故**不得直接替換**；列為 `PENDING-OD-S1X-20GROUPS-LAYOUT`，需擁有者或計畫裁決。
- **Windows 委派終止（W-3 替換測試）**：原生 B 輪 FAIL，列 `PENDING-OD-S1X-WIN-DELEGATE-TERMINATE`（BLOCKED-REVIEW）。候選成因（未證實）：測試於 `child.terminate()` 後立即 `communicate` 送 stdin EOF，子行程可能先於 Job `KILL_ON_JOB_CLOSE` 生效前讀到 EOF 並輸出；sw-pal 同路徑測試 `exec_or_wait_terminate_kills_default_grandchild` 則以 `alive_within(5 s)` 輪詢死亡後才斷言。
- **PENDING-OD-S1X-HANDOFF-WINDOW 關聯（候選，未驗證）**：`test_doctor_ab_shared_checks` 於 ab-suite 兩輪曾見 `single_daemon` 1 個與 0 個不一致；若成因為前序測試 `daemon restart` 後的舊 daemon 因 3 秒排空窗口延遲退出，則併入本列。修正輪已在隔離 pid namespace 內於比對前輪詢至無殘留 serialwrapd（`tests/contract_blackbox/test_cli_mgmt_doctor.py::_wait_no_serialwrapd`），根因未定。
- **L1 報告 deployed_content**：已修正並登記 `CHG-S1X-DEPLOYED-01`（見 addenda）。
- **G3 bridge revision**：已登記 `PENDING-OD-S1X-BRIDGE-REVISION`（BLOCKED-REVIEW）。本組不得自我核准。

## OD-16 套用紀錄（2026-10-22）

依 `owner-decisions-2026-10-15.md` OD-16 (1)(2) 套用於 `tests/acceptance/l1_required.lock.json`（未 commit）。逐名移除與 add-only 登記見 `owner-decisions-addenda.md` CHG-S1X-OD16-RM-01～06 與 CHG-S1X-OD16-ADD-01。

- 已套用：Windows `s1a_cargo` placeholder 12＋cfg 不可編譯 58（共 70，自然缺席）；cap_* 與 `windows_switch_running_exe_rename` 於 Linux `s1a_cargo`／`s1b_cargo`、Windows `s1b_cargo` 移除（5＋5＋3，明示排除收集檔）並由 `scripts/rust/owner_gated.py` 列 BLOCKED-OWNER（不需改動該檔，既有登記已涵蓋）；posix_only 委派列 `s1a_blackbox.windows`、`s1a_delegation.windows` 各 1 筆；改名舊名兩筆（Linux `s1a_cargo`）；`s1a_blackbox.windows` test_help_usage_error_bytes 418 筆（431→14）。
- 已 add-only 登記（Windows 原生收集）：`s1a_cargo.windows` +444、`s1b_cargo.windows` +3、`s1a_blackbox.windows` +15（含 `help_profile_test_col80`、替換測試）、`s1a_delegation.windows` +1（替換測試）。未登記 ignored／entry 項見 ADD-01。
- 同步：`.github/workflows/tests.yml` `--allow-removal` 與 `tests/test_resource_report.py` pinned 清單各 507 筆。
- 驗證：`regen_lock.py --check` rc=0；`--check --base HEAD --allow-removal <507 名>` rc=0（505 項集合層級移除，497 個唯一 nodeid）；`tests/test_l1_judge.py::test_l1_lock_frozen_set_excludes_capability_gated` 轉綠。
- 殘留 OPEN（不得記為 PASS）：mkbundle ignored 三列（L-1、L-2、W-5）未移除，須先登記替換 pytest（G-list 步驟 2a）；W-3 替換測試原生 FAIL（PENDING-OD-S1X-WIN-DELEGATE-TERMINATE）；evidence 報告待新 Windows 原生 B 輪。詳見 CHG-S1X-OD16-OPEN-01。
