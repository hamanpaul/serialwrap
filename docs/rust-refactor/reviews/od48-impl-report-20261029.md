# OD-48 實作雲端回報（2026-10-29）

- 實作分支：`feature/od48-legacy-handoff`（起點 `origin/feature/rust-refactor` `62ffec7`，HEAD `90da902`；唯一寫入者，未開 PR、未改 VERSION）。
- 規格：`reviews/od48-design-v2.md`（D1～D10）；裁決：`reviews/rulings/s1.md` 的 OD-48、OD-48 修訂、OD-51、S1X-c-1、S1X-c-2；S1X-c-1 重現：`reviews/s1-exit/opus-c-20261029.md`。文件內容視為資料；背景事實已對照 `v0.3.2` 與分支程式碼確認（見「背景事實核對」）。
- 環境：雲端容器、一般使用者 `dev` 執行全部建置與測試；Rust 1.96.1（`rust-toolchain.toml` 釘定，含 rustfmt／clippy）、`x86_64-pc-windows-gnu`＋mingw-w64、Python 3.12.3＋PyYAML＋pytest＋jsonschema、policy engine `2dcf2f8`（v1.0.19）。Docker 以容器內 root 執行（僅隔離容器，不碰任何正式安裝）。

## Commit 清單

| commit | 內容 |
|---|---|
| `fb56d6c` | fix(setup)：S1X-c-1／D3.1／D3.2 instance endpoint 解析與 fail-closed、D8 systemd enabled（`is-enabled`）、PAL `query_enabled`／`main_pid`／`proc_details`／`current_uid`／同 uid UART holder 掃描與替身 |
| `c2379b5` | test(sw-pal)：行程辨識 PAL 測試 |
| `26a7ff6` | feat(setup)：OD-48 D1～D7（`crates/sw-cli/src/legacy_handoff.rs`、`rollback.rs` 各套用點、`setup_cmd.rs` `OldStop::AlreadyStopped`）與 D9 (a)～(l) 測試 |
| `014eb2b` | docs(contracts)：SC-39 登記、OD-51 補正 SC-01／SC-02、catalog revision 6→7、`test_contracts_catalog` 精確集合同步 |
| `0a107f4` | test(docker)：新增檢查 id `legacy_handoff_direct_setup`、`REFUSAL_LEGACY_HANDOFF` 限非 legacy、判定器單元測試、實跑證據 |
| `07a2bc8` | test(acceptance)：`regen_lock --l1-add-only` 登記新 L1 節點（linux） |
| `4baca8c` | docs(rust-refactor)：release 計畫（§4.1／§5.1 含 S1X-c-2／§6／§7）、plan 05 Task 12、spec-reconciliation、owner-decisions OD-48～OD-51、README 中英 |
| `90da902` | test(acceptance)：`check_p4_cases --freeze --add-only` 登記 `sc39-legacy-daemon-handoff`（只追加 1 筆、凍結 commit 不變），`test_sc39_registered` 補 `@pytest.mark.case` |

## 背景事實核對

| 設計背景事實 | 核對結果 |
|---|---|
| 0.3.2 pipx 無 `current-release.json` → `op="install"`；殘留時 `op="update"` | 成立（`execute_change_with_restore` 依 `read_current` 互轉）。 |
| 拒絕路徑：`graceful_stop_with_existing_hold` → `request_handoff` → `Unconfirmed{METHOD_NOT_FOUND, outcome:"refused"}` | 成立。 |
| 0.3.2 `daemon.stop` 只設 stop_event 立即回 `{ok,stopping}` | 成立（`v0.3.2:sw_core/daemon.py` `_handle`）。未知 method 回 `{ok:false,error_code:METHOD_NOT_FOUND,method}`，`rpc()` 以 setdefault 補 `version`（`v0.3.2:sw_core/service.py`）。 |
| systemd unit `Restart=on-failure`、`RestartSec=2`；system unit 固定 `--socket /run/serialwrap/serialwrapd.sock`；user unit 不帶 `--socket` | 成立（`v0.3.2:sw_core/systemd_units.py`、`setup_cmd.py`）。另：system unit `User=` 為安裝者本人（`SUDO_USER` 或目前帳號），因此 D1.3 讀得到 `/proc/<pid>/exe`。 |
| P5～P7 Rust daemon 也回 `version:"0.3.2"` | 依設計列為前提；D1.3／D1.4 以行程正向辨識排除（測試 `legacy_b_native_exe_in_bundle_path_fails_closed`）。 |
| `query_supervisor` 以 `is-active` 推 enabled | 成立；另發現 `run_supervision_switch` 的 `enabled_before` 與 `stop_for_revert_with_hold` 同一根因，一併改用 `supervisor_enabled`。 |
| SC-37 為 P5、SC-38 為 OD-21 | 成立；另 SC-28～SC-33 由 plan 00／07／08／09 保留，全部遠端分支皆無 SC-39 → 採 SC-39。 |
| 0.3.2 session.list 欄位（foreground_busy、outstanding_commands、interactive_session_id、recovering、attached_real_path） | 成立；背景命令以 `outstanding_commands`（`background_cmd_ids`）反映。 |

## D1～D10 實作位置與測試

| 項目 | 實作位置 | 測試 |
|---|---|---|
| D1 判定 | `legacy_handoff.rs` `identify`、`version_is_legacy`、`under_versions`；PAL `proc_scan::is_python_interpreter`、`python_daemon_script`、`ProcScan::proc_details`（POSIX `/proc`、Windows Toolhelp＋`QueryFullProcessImageNameW`）；拒絕回應原文經 `graceful_stop::take_last_handoff_refusal` 取 `version` | `legacy_b_native_exe_in_bundle_path_fails_closed`、`legacy_b_python_script_under_versions_fails_closed`、`legacy_b_version_not_below_040_fails_closed`、`legacy_b_unknown_version_fails_closed`；PAL `python_interpreter_detection`、`python_daemon_script_detection`、`posix_proc_details_reads_ppid_uid_exe_argv_and_start_ticks`；凍結節點 `update_handoff_unsupported_daemon_refuses_without_stop`、`revert_handoff_unsupported_new_daemon_keeps_journal`、`supervision_set_handoff_unsupported_refuses_without_stop` 期望不變且通過 |
| D2 套用範圍 | `rollback.rs` `stop_supervisor`（op≠rollback）→ `stop_legacy_daemon`；install 後段 reconcile 以 `OldStop::AlreadyStopped`＋`daemon_running=false`；`stop_for_revert_with_hold` 的 legacy 參數（回退與 `recover_journal`）；`supervision-mode set` 不傳 legacy 能力 | `legacy_a_*`、`legacy_f_post_switch_failure_restores_install_without_restart`、`legacy_l_crash_loop_recover_journal`、凍結 `supervision_set_handoff_unsupported_refuses_without_stop` |
| D3.1 端點 | `endpoint_resolve::instance_endpoint`、`target_instance_endpoint`；`execute_change_with_restore`（停舊／啟新分開）、`revert_after_failure`、`run_supervision_switch` | `s1x_c1_update_detects_systemd_system_daemon_on_fixed_socket`、`s1x_c1_first_install_detects_systemd_system_daemon_on_fixed_socket`、`s1x_c1_supervision_switch_detects_systemd_system_daemon_on_fixed_socket`、`legacy_i_systemd_system_fixed_socket_detected_for_update_and_install` |
| D3.2 基準掃描 | `safety_check`（ping 不通 → 同 uid serialwrapd 一律 `DEVICE_STILL_HELD`＋`pids`／`sockets`）、`same_uid_daemons`；systemd running 無 RPC daemon → fail-closed；`legacy_handoff::precheck` 目標外同 uid → `other_daemon_present`、他 uid 標註 `other_uid_pids` | `d3_2_foreign_socket_daemon_without_rpc_fails_closed`、`d3_2_supervisor_running_without_rpc_daemon_fails_closed`、`legacy_h_other_same_uid_daemon_fails_closed_before_stop`、`legacy_h_other_uid_daemon_is_noted_not_blocking` |
| D3.3 忙碌 | `precheck` 送 stop 前再跑 `safety_check`（`mcu.status`／`session.list`） | `legacy_c_flashing_at_precheck_refuses_without_stop`、`legacy_c_session_busy_at_precheck_refuses_without_stop` |
| D3.4 外部監管者 | `precheck` 父行程檢查（MainPID 不相符時） | 無專用 fake 測試（見 SPEC-GAPS 3、LOCAL-WINDOWS-TODO 1） |
| D3.5 擷取檔 | `legacy_handoff::capture_record`、`rollback.rs` `JournalPersist`（`install-journal.legacy.json` atomic write＋`sync_dir`、sha256 寫進 journal `legacy{sha256,stage}`，`clear_journal`／`finish_recovery` 清除） | `legacy_d_never_exits_is_pending_without_restart`（擷取檔與 journal stage 保留）、`legacy_l_crash_loop_recover_journal` |
| D4 停止手段 | `precheck`（`ServiceCtl::main_pid` user＋system 都查）、`send_stop`（`systemctl stop` 或 RPC `daemon.stop` 2 s；無 hard stop） | `legacy_a_systemd_user_main_pid_uses_systemctl_stop`、`legacy_g_main_pid_mismatch_uses_rpc_stop`；PAL `systemd_system_query_enabled_and_main_pid_never_use_sudo`、`systemd_main_pid_zero_means_no_main_process` |
| D5 已停判準 | `legacy_handoff::wait_stopped`（行程樹、`uart_holders_same_uid`、基準差分、5 s 穩定窗、330 s 上限、`restop` 最多 3 次）；啟動前閘門（`release_held_checked`／`set_enabled` 之後）以基準差分 | `legacy_a_on_demand_install_stops_with_rpc_and_setup_starts_new_daemon`（socket 先消失、行程 40 s 後才結束，`waited_ms≥45000`）、`legacy_d_*`、`legacy_e_relaunch_loop_after_three_stops`、`legacy_e_single_relaunch_is_restopped`、`legacy_k_relaunch_inside_stability_window_is_not_stopped`；PAL `posix_uart_holder_scan_covers_any_same_uid_process_and_skips_other_uids` |
| D6 失敗還原 | `stop_legacy_daemon`（stop 前：撤銷 hold；stop 後切換前：撤銷 hold、不啟動）、`annotate_legacy`、`revert_after_failure`／`restore_supervisor_and_daemon(no_restart)`、`recover_journal`（pid＋start_ticks 仍在 → `legacy_stop_pending`；已結束 → 還原不重啟）、`legacy_handoff::restart_hint` | `legacy_f_post_switch_failure_restores_install_without_restart`、`legacy_f_pre_switch_failure_releases_hold_without_restart`、`legacy_e_relaunch_loop_after_three_stops`、`legacy_d_*`、`legacy_l_*` |
| D7 成功路徑 | `stop_supervisor` 回 `legacy_stop`；`start_and_verify(original.daemon.is_some())` 啟動新 daemon | `legacy_a_*`；Docker `legacy_handoff_direct_setup` |
| D8 enabled | PAL `ServiceCtl::query_enabled`（systemd `is-enabled`；`parse_is_enabled`）、`rollback.rs` `supervisor_enabled`、`hold_prior_enabled` | `d8_disabled_running_unit_stays_disabled_after_update`、`d8_disabled_running_unit_stays_disabled_after_refusal`；PAL `is_enabled_mapping_distinguishes_disabled_masked_and_static`、`systemd_query_enabled_uses_is_enabled_not_is_active` |
| D9 契約／測試／文件 | SC-39、OD-51、L1 add-only、Docker、文件（見各節） | `test_sc39_registered`、`test_sc01_sc02_cover_file_transfer_per_od51`、`test_legacy_direct_setup_*`、`test_legacy_refusal_no_longer_accepted_for_0_3_x` |
| D10 S1X-c-1 | 併入 D3.1／D3.2／D9(i) | 同上 |

TDD：D8 與 S1X-c-1／D3.2 的 7 筆測試先紅後綠（逐筆確認紅燈後才改程式）。D1～D7 的 27 筆中，以「新替身＋新測試＋實作前的 `sw-cli/src`」另建 worktree 實跑取證：14 筆紅（所有 legacy 停止行為），13 筆綠（fail-closed 期望與 D8／S1X-c-1 已修部分，舊程式本就滿足）。

## 測試與 gate 數字（最終 HEAD `90da902`）

| 項目 | 起點基準（`62ffec7`） | 最終 |
|---|---|---|
| `cargo test -p sw-cli -p sw-pal --features test-fakes` | 765（764 passed＋1 環境性失敗：PATH 含無權限目錄致 `sysenv_mgmt::run_missing_exe_is_notfound_with_py_message` 回 PermissionDenied；修正 PATH 後通過，非產品問題） | **803 passed／0 failed**（＋38 新測試） |
| `cargo test --workspace --all-features` | — | **943 passed／0 failed／7 ignored** |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 通過 | 通過（0 error；每組 commit 皆跑） |
| `python3 scripts/rust/check_cfg_boundary.py` | 通過 | 通過（platform cfg 只在 sw-pal） |
| `python3 -m tools.contracts check` | ok | ok（SC 29→30、IMPLEMENTED 49→50） |
| `python3 -m policy_check --repo .` | rc 0（R-19 warn） | rc 0（R-19、R-22 warn；R-22 為既有 2881 筆懸空引用的 advisory） |
| `regen_lock.py --check`／`--check --base origin/feature/rust-refactor` | OK | OK |
| 觸及的 Python 測試（`test_contracts_catalog`、`test_contracts_validate`、`test_p1oc_catalog`、`test_check_p4_cases`、`test_rust_p4_case_map`、`test_docker_upgrade -m memory_only`、`test_contracts_doc_errata`、L1 相關 6 檔） | — | 全綠，只剩下列基準既有失敗 |
| 全套 `python3 -m pytest -q tests/` | **8 failed／5284 passed／168 skipped／1 xfailed** | **8 failed／5290 passed／168 skipped／1 xfailed**；失敗集合與起點逐筆相同（新增 0、消失 0） |

起點基準的 8 筆失敗（全部為既有、與本分支無關）：`test_doctor::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_stage0_tx_outcome::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit::test_pyserial_chunked_write_drain_stops_after_inflight_chunk`、`test_stage0_tx_permit::test_pyserial_partial_cancel_keeps_cancelled_reason`、`test_stage0_uart_exclusive::TestManagerAttach::test_pyserial_access_denied_raises_device_held_unknown`（容器未裝 pyserial）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`（需 Windows 原生）、`test_l1_judge::test_l1_lock_nodeids_subset_of_collect_only`、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`（需 P4 正式證據目錄）。

中途 HEAD `4baca8c` 的全套多出 1 筆 `test_rust_p4_case_map::test_p4_case_map_covers_all_p4_behavior_changes`（SC-39 phase P4 尚未進 case map，屬真實缺口而非 flaky），已由 `90da902` 以官方 add-only 程序修正並單獨重跑確認。

## 新 L1 節點（`s1a_cargo.linux` 與 `s1b_cargo.linux` 各 38 筆，`regen_lock --l1-add-only`，移除 0）

collect 由 `scripts/rust/cargo_l1_results.py --list-only`（`--workspace`／`--workspace --all-features`）產生，僅保留既有鎖內節點與本分支新節點後餵給 `regen_lock`；`regen_lock --check` 與 `--check --base origin/feature/rust-refactor` 皆 OK。兩集合新增的節點相同：

- `cargo:sw-cli::mgmt_legacy_handoff::d3_2_foreign_socket_daemon_without_rpc_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::d3_2_supervisor_running_without_rpc_daemon_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::d8_disabled_running_unit_stays_disabled_after_refusal`
- `cargo:sw-cli::mgmt_legacy_handoff::d8_disabled_running_unit_stays_disabled_after_update`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_a_on_demand_install_stops_with_rpc_and_setup_starts_new_daemon`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_a_systemd_user_main_pid_uses_systemctl_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_b_native_exe_in_bundle_path_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_b_python_script_under_versions_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_b_unknown_version_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_b_version_not_below_040_fails_closed`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_c_flashing_at_precheck_refuses_without_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_c_session_busy_at_precheck_refuses_without_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_d_never_exits_is_pending_without_restart`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_e_relaunch_loop_after_three_stops`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_e_single_relaunch_is_restopped`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_f_post_switch_failure_restores_install_without_restart`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_f_pre_switch_failure_releases_hold_without_restart`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_g_main_pid_mismatch_uses_rpc_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_h_other_same_uid_daemon_fails_closed_before_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_h_other_uid_daemon_is_noted_not_blocking`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_i_systemd_system_fixed_socket_detected_for_update_and_install`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_k_relaunch_inside_stability_window_is_not_stopped`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_l_crash_loop_recover_journal`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_l_crash_points_are_separate_from_frozen_list`
- `cargo:sw-cli::mgmt_legacy_handoff::s1x_c1_first_install_detects_systemd_system_daemon_on_fixed_socket`
- `cargo:sw-cli::mgmt_legacy_handoff::s1x_c1_supervision_switch_detects_systemd_system_daemon_on_fixed_socket`
- `cargo:sw-cli::mgmt_legacy_handoff::s1x_c1_update_detects_systemd_system_daemon_on_fixed_socket`
- `cargo:sw-pal::legacy_supervision::fake_proc_details_follow_liveness`
- `cargo:sw-pal::legacy_supervision::fake_service_main_pid_and_enabled_are_scriptable`
- `cargo:sw-pal::legacy_supervision::is_enabled_mapping_distinguishes_disabled_masked_and_static`
- `cargo:sw-pal::legacy_supervision::non_systemd_backends_do_not_claim_enabled_or_main_pid`
- `cargo:sw-pal::legacy_supervision::posix_proc_details_reads_ppid_uid_exe_argv_and_start_ticks`
- `cargo:sw-pal::legacy_supervision::posix_uart_holder_scan_covers_any_same_uid_process_and_skips_other_uids`
- `cargo:sw-pal::legacy_supervision::python_daemon_script_detection`
- `cargo:sw-pal::legacy_supervision::python_interpreter_detection`
- `cargo:sw-pal::legacy_supervision::systemd_main_pid_zero_means_no_main_process`
- `cargo:sw-pal::legacy_supervision::systemd_query_enabled_uses_is_enabled_not_is_active`
- `cargo:sw-pal::legacy_supervision::systemd_system_query_enabled_and_main_pid_never_use_sudo`

未順帶登記的既有未登記節點（不屬本分支）：`s1b_cargo.linux` 4 筆、`s1a_cargo.linux` 9 筆（`sw-pal::service_ctl::systemd_*_dropin_dir*` 4、`sw-cli::mgmt_daemon::autostart_resend_carries_original_command_params`、`start_windows_python_launcher_*` 2、`sw-pal::proc_scan::posix_tty_holder_scan_*` 2）。

## Docker（有，已實跑兩種監管模式）

- 容器內 dockerd 可啟動；`ubuntu:24.04@sha256:68ee2200…` 可拉取。代理只放行 HTTPS，`tools/docker/upgrade.Dockerfile` 的 apt 明文 HTTP 不通，故在 scratch 以同一 Dockerfile 加「apt 來源改 https＋信任 proxy CA」建映像（與審查 C 相同作法），`run-upgrade.sh` 的 scratch 副本只把 `docker build` 換成 `docker tag`；driver、判定、`--network none`、mount 全部不變。scratch 檔案未提交。
- 第一次實跑無效：host `python3` 為 3.11，`pip download` 抓到 cp311 PyYAML，容器內 3.12 的 pipx 安裝 0.3.2 失敗（環境問題，報告未採用）。改以 Python 3.12 的 `python3` 重跑。
- run `od48run2`（wheel：`git archive v0.3.2` 建置；bundle：`mkbundle.sh --release-bundle --profile release --version-override 0.4.0`，git revision `014eb2b`）：**systemd-user PASS、on-demand PASS**，19 項必要檢查全 PASS。
  - `legacy_handoff_direct_setup`：systemd-user `method=systemctl_stop`、on-demand `method=rpc_daemon_stop`；`from_version=0.3.2`、`waited_ms` 5235／5215、`relaunch_count=0`、`started_by_setup=true`、`old_alive_after=[]`、`daemon_version_after=0.4.0`、新 daemon 唯一。
  - `no_two_reader`：取樣 1576／1531、0 次重疊。觀察項：`OBS-UPGRADE-ROLLBACK-PIPX`、`OBS-UPGRADE-STALE-CURRENT`（OD-49 已知限制）、on-demand 的 `OBS-0.3.2-ONDEMAND-START`。
  - 報告與 `SHA256SUMS` 存於分支 `evidence/stage1/upgrade-docker/od48-014eb2b/`。屬開發中 bundle，不取代 release 前置條件 #3 的 release bundle 重跑。

## SPEC-GAPS

1. **D3.2「query_supervisor 顯示 running 卻無可 RPC daemon → fail-closed」只套 systemd 模式**：套到 windows-task 會改變 13 個凍結節點（見 OWNER-BATCH 1），依規則停下該部分。
2. **D4「user 與 system 兩種都查」與 hold 的模式不一致**：MainPID 相符的 unit 模式若不是 config 的舊模式，autostart hold 只握在舊模式上；設計未說明要不要另取該模式的 hold。實作為停止前 fail-closed（`DEVICE_STILL_HELD`、reason `supervisor_mode_mismatch`，hint 要求以 `--user`／`--system` 指定實際模式），未自行加第二個 hold。
3. **D3.4 外部監管者以父行程判定的限制**：被重新收養給 systemd／init 的 on-demand 孤兒行程，與被「另一個 systemd unit」包起來的 daemon，父行程相同、無法區分。實作以父行程 basename 清單判定（supervisord、runsv、s6-supervise、supervise、monit、circusd、pm2、forever、nssm、services、taskhostw、taskeng、svchost，及 argv 含 supervisord 的 python），非 serialwrap.service 的自訂 unit 靠 D5 重拉偵測（最多 3 次後 `relaunch_loop`）兜底。若要更準確須讀 `/proc/<pid>/cgroup`，屬設計增補。此項沒有專用 fake 測試。
4. **D5「最後閘門移到 release_held_checked／set_enabled 之後」**：只在 legacy 路徑新增該閘門（D3 基準差分）；Rust 對 Rust 路徑原有的 `ensure_released_or_rehandoff`（release 之前）保持原位，未移動，以免改動凍結的事件順序期望（未驗證移動後是否仍綠）。
5. **D6「還原本身失敗 → 報告列出未還原項」**：`revert_pending=true`、journal 保留、`revert_error.message` 帶失敗原因，但沒有逐項列出未還原的項目。
6. **D3.3「背景 capture」**：0.3.2 的背景命令由 `session.list` 的 `outstanding_commands`（`background_cmd_ids`）反映，沿用既有 `busy_session`；agent log capture（`capture` 欄位）不視為忙碌。
7. **D5.2 UART 範圍**：holder 掃描路徑取自停止前檢查時 `session.list` 的 `attached_real_path`；沒有附掛路徑的 session 不掃。
8. **D1.2 版本取自拒絕回應**：為不改動 `HandoffError` 公開型別（凍結測試以模式比對），以 `graceful_stop` 的 thread-local 保存最近一次 `daemon.handoff_stop` 拒絕回應原文。
9. **文件路徑**：設計寫 `docs/rust-refactor/spec-reconciliation.md`，實際檔案在 `docs/rust-refactor/plans/spec-reconciliation.md`（已改後者）。
10. **systemd-system 成功路徑**：fake 測試只覆蓋拒絕（D9(i)）；沙箱沒有 `/etc/systemd/system/serialwrap.service`，`backup_state` 無法建立快照，成功路徑未以 fake 驗證；`run-upgrade.sh` 也只有 systemd-user／on-demand 兩種模式。
11. **D1.3 Windows 判定**：除 PyInstaller one-file（`serialwrapd.exe` 父＋子）外，也把 pipx distlib launcher（`serialwrapd.exe`＋`python.exe` 子行程）視同 launcher＋子行程樹；子行程 exe／script 在 `versions/` 底下即排除（0.4.0 Windows bundle 也是 launcher＋bundled python 子行程）。

## OWNER-BATCH

1. **D3.2 windows-task 的 running-without-RPC fail-closed**：套用會改變 `crates/sw-cli/tests/mgmt_com0com.rs` 13 個凍結節點的期望（fake windows-task 回報 Running 但沒有 RPC daemon，首次安裝／更新改為 `DEVICE_STILL_HELD`）：`base_install_never_elevates`、`component_absent_with_flag`、`existing_pair_not_reinstalled`、`experimental_stderr_line`、`helper_failure_reported`、`plain_setup_reports_existing_pair_without_elevation`、`registry_probe_error_does_not_skip_elevated_install`、`remove_com0com_plan`、`rollback_does_not_touch_driver`、`standalone_remove_failure_is_nonzero`、`uac_declined_keeps_base_ok`、`update_reports_existing_pair_even_when_new_bundle_omits_component`、`with_com0com_elevates_exactly_helper`。需擁有者決定：(a) 維持只套 systemd；(b) 套用到 windows-task 並修訂這 13 個節點的 fake 設定。
2. **D4 模式不一致**（SPEC-GAPS 2）：是否改為另取實際 unit 模式的 hold 後 `systemctl stop`，或維持 fail-closed。
3. **D3.4 是否改以 cgroup 判定外部監管者**（SPEC-GAPS 3）。

## LOCAL-WINDOWS-TODO

1. `WindowsProcScan::proc_details`（Toolhelp 父 pid、`QueryFullProcessImageNameW`、命令列、建立時間）與 D1.3 的 one-file 父子行程、pipx launcher 判定，須在 Windows 原生實測（目前僅 `cargo check --target x86_64-pc-windows-gnu`）。
2. NSSM、服務（services.exe）、Task Scheduler（taskhostw／svchost Schedule）包起來的 0.3.2 daemon，D3.4 停止前 fail-closed 的實機驗證。
3. Windows 0.3.2（PyInstaller one-file 或 pipx）→ 0.4.0 在 daemon 執行中直接 setup 的實跑（release 計畫 §4.2），含 `holders_status: unsupported` 標註。
4. `s1a_cargo.windows`、`s1b_cargo.windows` 的 add-only 登記須以 Windows 原生收集（WSL 不得代替）；新節點中 `legacy_supervision` 的 `cfg(unix)` 測試在 Windows 不存在。
5. Windows 原生 `cargo test` 執行新測試（`mgmt_legacy_handoff` 以 `TestEnv::posix()` 為主，路徑判定在 Windows 主機上的行為需確認）。

## 未完成項

- OWNER-BATCH 1～3 待裁決後處理。
- LOCAL-WINDOWS-TODO 1～5。
- `sc39-legacy-daemon-handoff` 的 P4 正式證據（`target/p4-evidence/sc39-legacy-daemon-handoff.json`）未產生；`test_p4_required_cases_have_passing_evidence` 在本環境與起點同樣失敗（整個 P4 證據目錄缺席）。
- release bundle（非 `--version-override`）的 Docker 重跑屬 release 前置條件 #3，未在本次範圍。
- 既有檔案未全面 rustfmt（repo 本身未維持 rustfmt 乾淨，`cargo fmt --check` 起點即有差異）；本分支不重排既有檔案。
