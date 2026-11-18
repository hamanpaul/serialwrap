# OD-105：Linux 完整 pytest 必要集合逐名移出分類

> 本表是 `od98-linux-pytest-skipped-audit.md` 的 OD-105 補充。分類依擁有者裁決 OD-105（2026-11-18；來源識別 commit `04be1dd49f912e8c2c0024c2f08e6ac106371333`，檔案 `docs/rust-refactor/reviews/rulings/s2.md`）第 1～4 類；每個 nodeid 均列出分類與承擔集合／理由。只改兩個 Linux L1 required lock，不刪測試、不改 pytest selector，也不宣稱被指派集合已於本候選版通過。

## 分類鍵

| 鍵 | OD-105 分類與承擔／理由 |
|---|---|
| `1-L15` | 分類 1：`s21_gate:l15_ab`（`slice21_gate.sh` 的 Task 21 L1.5 A/B，實跑 `tests/acceptance/l15/test_slice21_l15.py`，含 Python 與 Rust 兩輪）。不應誤寫成僅含 Rust node 的 `p7_gate:l15-slice21-b`。H*（`ba29434c`）只作參考，候選版仍須以共用證據重驗。 |
| `1-S24-RUST` | 分類 1：`s24_ab`（`gate_shared.sh` 以 label `rust-linux` 執行 `S24_TARGETS`，含 `tests/contract_blackbox`；DF-20 案例在 `s21_gate:blackbox_rust`（label `rust-s21`）與 P7 slice21 B 輪依 `ab_rows.py` 為 `deferred_rust_s21` skip，只在 `s24_ab` 實跑）。H*（`ba29434c`）`s24_ab` 的 rust-linux 結果為 passed，僅作參考，候選版須以共用證據重驗。 |
| `2-S24` | 分類 2：Rust 專屬 CLI 黑箱由 `s24_ab` 承擔；實際共用命令目標含 `tests/contract_blackbox`。H*（`ba29434c`）僅為參考，候選版須重驗。 |
| `s0_win_pytest.windows` | 分類 3：Stage 0 原生 Windows 全套 Python selector（`-q tests -m 'core_portable or windows_native'`）；selector 覆蓋整個 `tests/`。若表列 node 不在該 set 的凍結 L1 nodeids，仍可能由完整 selector 收集；不因此宣稱列入 required 或已 PASS。 |
| `s0_bc.windows` | 分類 3：Stage 0 BC-01～BC-05 的 Windows 原生 L1 set（`S0-L1-BC`）；表列 node 依其實際 lock membership 註明。此集合尚未取得目前候選版 Windows 原生執行結果，不代表 PASS。 |
| `s1a_blackbox.windows`／`s1a_delegation.windows`／`s1b_20groups.windows` | 分類 3：Stage 1 原生 Windows 黑箱 selector；逐列註明實際適用集合。未列入凍結 L1 required nodeids 的額外 selector 項，也不因此宣稱已 PASS。 |
| `s21_gate:py_parity.windows` | 分類 3 特例：F-S19 Windows COM 原生 Python node 的專屬 Stage 2 Windows set；只承認原生實跑結果，不以 collect-only 代替。 |
| `s24-ho-windows-com` | 分類 3 特例：Windows COM 的 S24 必要 acceptance case；三個對應測試函式皆以此 case id 記錄 observation，必要 case 定義見 `tests/acceptance/cases.yaml`。 |
| `S0-SPIKE1-REMAINING` | 分類 3 特例：Spike 1 COM 原生測試的 Stage 0 evidence id；須在擁有者 host、指定 COM pair 實跑，不把舊 evidence 自動當候選版 PASS。 |
| `4-TXRX` | 分類 4：Linux 全套 pytest 不可執行；需設定 `SERIALWRAP_TEST_SERIAL_PORT` 指向 TxRx 短接的實體序列埠；Stage 2 無真機承擔。 |
| `4-RE2` | 分類 4：Linux 全套 pytest 不可執行；缺選用套件 `re2`，且 Stage 2 無承擔。 |
| `4-PWSH` | 分類 4：Linux 全套 pytest 不可執行；需 `pwsh`／`powershell.exe` 及 `-WhatIf` 動態斷言，Stage 2 無承擔。 |
| `4-DACL` | 分類 4：Linux 全套 pytest 不可執行；需 Windows DACL 原生語意，Stage 2 無承擔。 |
| `4-CHILD` | 分類 4：Linux 全套 pytest 不可獨立執行；僅由父測試啟動的子行程執行，Stage 2 無承擔。 |
| `4-XFAIL` | 分類 4：P3-T18 的 xfail；OD-105 裁決移出 required，但測試仍為 xfailed、不得計 PASS；Stage 2 無承擔。 |

## 逐名分類

| nodeid | 分類 | 承擔集合鍵／理由鍵 |
|---|---:|---|
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-exclusive-open]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-half-prompt]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-multi-source-contention-cli]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-reboot-min-guard]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-reject-zero-tx]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-release-inflight]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-slow-reader-backpressure]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-stale-event-delay]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-l15-transport-disconnect]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-credentials-unresolved]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-exclusive-conflict-plain-open]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-exclusive-conflict]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-partial-tx-abort]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-quiet-zero-tx]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-release-attach-handoff]` | 1 | `1-L15` |
| `tests/acceptance/l15/test_slice21_l15.py::test_case[python-SERIALWRAP_L15_PY_DAEMON-s21-wire-equals-wal]` | 1 | `1-L15` |
| `tests/contract_blackbox/slice21/test_safety_core.py::test_stale_context_not_replayed` | 1 | `1-S24-RUST` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_config_yaml_socket_path_written` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_deferred_methods_windows` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_second_daemon_exit_2` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_session_public_console_endpoint_null` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_stop_exit_0` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_tcp_endpoint_loopback_and_ping` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/slice21/test_windows_nocom.py::test_wal_share_delete_while_running` | 3 | `s0_win_pytest.windows` |
| `tests/contract_blackbox/test_bb_delegation.py::test_exec_failure_runs_nothing_and_does_not_fall_back` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_exec_replaces_process_and_sigterm_reaches_child` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_missing_manifest` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_never_searches_path` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_no_output_before_delegation` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_probe_receives_argv_env_stdin_and_exit_code[0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_probe_receives_argv_env_stdin_and_exit_code[1]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_probe_receives_argv_env_stdin_and_exit_code[37]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_recursion_detected_when_entry_points_to_self` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_delegation.py::test_windows_exec_or_wait_exit_code_and_terminate_propagation` | 3 | `s1a_blackbox.windows`／`s1a_delegation.windows` |
| `tests/contract_blackbox/test_bb_exit_codes.py::test_non_utf8_bench_echo` | 2 | `2-S24` |
| `tests/contract_blackbox/test_bb_skill.py::test_skill_default_platform_windows` | 3 | `s1a_blackbox.windows` |
| `tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_suite_execution` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_daemon.py::test_autostart_l15_tx_once` | 3 | `s1a_blackbox.windows`／`s1b_20groups.windows` |
| `tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_start_releases_caller_merged_stdio_pipe` | 3 | `s1a_blackbox.windows` |
| `tests/contract_blackbox/test_cli_mgmt_daemon.py::test_daemon_start_releases_caller_stdio_pipes` | 3 | `s1a_blackbox.windows` |
| `tests/contract_blackbox/test_cli_mgmt_l15.py::test_autostart_l15_tx_once` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_l15.py::test_restart_no_reopen_before_handoff` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_l15.py::test_update_busy_l15_zero_tx` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_l15.py::test_update_file_push_after_last_probe_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_l15.py::test_update_push_after_freeze_zero_tx_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_remote.py::test_remote_sigint_cleans_spawned_tunnel` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_setup.py::test_supervision_set_rust_only` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_supervision_switch.py::test_supervision_set_flashing_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_supervision_switch.py::test_supervision_set_force_push_continues_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_supervision_switch.py::test_supervision_set_rust_only_switch` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_terminal.py::test_minicom_wrapper_attach_detach_via_rust_cli` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_terminal.py::test_minicom_wrapper_auto_start_daemon_on_demand` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_terminal.py::test_minicom_wrapper_sigterm_still_detaches` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_fresh_install_blackbox` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_revert_new_daemon_push_not_aborted_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_background_triggers_zero_tx_in_drain_window_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_busy_l15_zero_tx` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_file_push_after_last_probe_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_push_after_freeze_zero_tx_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_rollback_ab_isolated` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_mgmt_update.py::test_update_systemd_exit1_not_relaunched_l15` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[--help]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[--version]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[alias list]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[benches]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[cmd submit --selector COM0 echo 1]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[connect NOPE]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[daemon status]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[device list]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[doctor]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[event tail]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[file push --selector COM0 --local nonexistent --remote x]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[log status --selector COM0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[mcu status --selector COM0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[profile test --profile prpl-template]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[remote status]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[service status]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[session console-attach --selector COM0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[session console-detach --selector COM0 --client-id c1]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[session list]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[setup --on-demand]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[skill]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[stream status --selector COM0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[supervision-mode]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_cli_no_delegation.py::test_every_mgmt_group_is_native[wal tail --selector COM0]` | 2 | `2-S24` |
| `tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_access_denied_holder_unknown` | 3 | `s24-ho-windows-com` |
| `tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_external_flasher_cycle` | 3 | `s24-ho-windows-com` |
| `tests/contract_blackbox/test_s24_device_handoff_windows.py::test_windows_selftest_released_holder_unknown` | 3 | `s24-ho-windows-com` |
| `tests/test_concurrency_races.py::test_matcher_re2_falls_back_for_backreference` | 4 | `4-RE2` |
| `tests/test_concurrency_races.py::test_matcher_re2_flags_and_groups` | 4 | `4-RE2` |
| `tests/test_concurrency_races.py::test_matcher_re2_immune_to_polynomial_redos` | 4 | `4-RE2` |
| `tests/test_lock_win.py::test_stale_endpoint_acquires` | 3 | `s0_win_pytest.windows` |
| `tests/test_lock_win.py::test_two_locks_same_path_second_raises` | 3 | `s0_win_pytest.windows` |
| `tests/test_p1oc_drain_stalled.py::TestWindowsNativeDrainStalled::test_windows_guarded_write_stall_drain_stalled_native` | 3 | `s21_gate:py_parity.windows` |
| `tests/test_rust_agent_perm_verifier.py::test_windows_owner_with_denied_write_is_not_blocked` | 4 | `4-DACL` |
| `tests/test_rust_no_onefile_windows.py::test_windows_watcher_clean_cli_passes` | 3 | `s0_win_pytest.windows` |
| `tests/test_rust_no_onefile_windows.py::test_windows_watcher_flags_create_then_delete` | 3 | `s0_win_pytest.windows` |
| `tests/test_rust_p4_conftest.py::test_child_is_rust_round` | 4 | `4-CHILD` |
| `tests/test_rust_spike2_tools.py::test_whatif_schtasks_only_throwaway_task_name` | 4 | `4-PWSH` |
| `tests/test_serial_port.py::TestPySerialLoopback::test_alive` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestPySerialLoopback::test_fileno_is_none` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestPySerialLoopback::test_idle_read_returns_empty` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestPySerialLoopback::test_loopback_roundtrip` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestPySerialLoopback::test_set_baud` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestUARTBridgeLoopback::test_binary_byte_exact_echo` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestUARTBridgeLoopback::test_clean_stop_is_prompt` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestUARTBridgeLoopback::test_command_echo_via_wait_for_regex` | 4 | `4-TXRX` |
| `tests/test_serial_port.py::TestUARTBridgeLoopback::test_snapshot_alive_running` | 4 | `4-TXRX` |
| `tests/test_slice21_gate_judge.py::test_od68_removed_linux_capability_set_is_evidence_missing[windows_native]` | 3 | `s0_win_pytest.windows` |
| `tests/test_spike1_remaining.py::TestSpike1NativeCom::test_spike1_console_pair_native` | 3 | `S0-SPIKE1-REMAINING`／`s0_win_pytest.windows` |
| `tests/test_spike1_remaining.py::TestSpike1NativeCom::test_spike1_control_lines_native` | 3 | `S0-SPIKE1-REMAINING`／`s0_win_pytest.windows` |
| `tests/test_spike1_remaining.py::TestSpike1NativeCom::test_spike1_peer_close_native` | 3 | `S0-SPIKE1-REMAINING`／`s0_win_pytest.windows` |
| `tests/test_stage0_f12_oracle.py::test_native_reader_does_not_block_rotate` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_fakes.py::test_windows_native_marker_only_runs_on_nt` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_platform_mgmt.py::test_native_windows_lock_uses_lockfileex` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_remote_windows.py::test_native_filelock_windows_blocks_second_holder` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_remote_windows.py::test_native_spawn_alive_terminate_tree` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_uart_exclusive.py::TestWindowsNativeCom::test_native_second_open_access_denied` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_wal_av_sim.py::test_native_av_sim_helper_holds_wal_and_writer_completes` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_wal_av_sim.py::test_native_av_sim_oplock_variant_keeps_wal_writes_and_rotation_live` | 3 | `s0_win_pytest.windows` |
| `tests/test_stage0_wal_handle.py::test_native_line_ending_is_lf_only` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_rotate.py::test_native_rotate_blocked_by_reader_degrades` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_rotate.py::test_native_rotate_with_open_handle_succeeds` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_unlink.py::test_native_classic_delete_pending_with_external_reader` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_unlink.py::test_native_posix_semantics_delete` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_handle_allows_rename_and_delete_while_open` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_identity_changes_after_replace` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_lockfileex_conflicts_with_msvcrt_locking` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_os_remove_semantics_recorded` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_path_identity_missing_returns_none` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_reader_without_share_delete_blocks_rename` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_sync_dir_is_documented_noop` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_sync_on_append_handle` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_stage0_wal_winfs.py::test_native_windows_walfs_unlinked_reports_delete_pending` | 3 | `s0_bc.windows`／`s0_win_pytest.windows` |
| `tests/test_win_cli_endpoint.py::TestLocalDefaultEndpoint::test_auto_on_windows_uses_default_endpoint` | 3 | `s0_win_pytest.windows` |
| `tests/test_win_daemon_start.py::TestWindowsDetachFlags::test_popen_uses_detached_creationflags` | 3 | `s0_win_pytest.windows` |
| `tests/test_win_task_throwaway.py::test_cleanup_removes_only_throwaway` | 3 | `s0_win_pytest.windows` |
| `tests/test_win_task_throwaway.py::test_generated_launcher_redirects_stderr_and_obeys_deadline` | 3 | `s0_win_pytest.windows` |
| `tests/test_win_task_throwaway.py::test_task_xml_whatif` | 3 | `s0_win_pytest.windows` |
| `tests/test_rust_readme_trust_scope.py::test_no_same_user_isolation_claim_in_docs_and_help` | 4 | `4-XFAIL` |

分類合計：1 類 17、2 類 56、3 類 51、4 類 16；共 140 個 nodeid（139 個 skipped + 1 個 xfailed）。
