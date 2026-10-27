# S1X 凍結鎖既有 36 筆移除盤點

> **狀態：RESOLVED（OD-23，2026-10-27）**——擁有者核准下列 36 個 `set:platform:nodeid` 三元組全部補授權（P4 佔位 24、S1B 改名 2、Windows 平台不適用 10）；替換測試與不適用理由依本清單，Windows 不適用者在 Linux 集合仍保留原測試。36 個三元組均已列於 `.github/workflows/tests.yml` 的 `--allow-removal` 與 `tests/test_resource_report.py` 的 pinned 清單，`regen_lock.py --check --base` 對 `1e24d68^`、`1ea2bdc^`、`e25aec0^` 皆通過。下文「建議補授權」欄保留為裁決前的稽核原文。

本清單依各移除 commit 與其直接 parent 的 `tests/acceptance/l1_required.lock.json` 三元組差異產生。三組共 36 筆：`1e24d68` 24 筆、`1ea2bdc` 2 筆、`e25aec0` 10 筆。此文件只供擁有者裁決；本輪沒有修改凍結鎖，也沒有重新產生鎖檔。

## 分類結論

36 筆均列為「建議補授權」：既有提交與決策紀錄各自給出占位案例替換、測試改名或平台不適用的理由，符合 OD-16 所列的案例類型；但目前沒有 OD-16／OD-17 對這 36 個精確 `set:platform:nodeid` 三元組的授權。建議擁有者逐項決定是否補列精確授權。沒有找到足以判為「建議還原」的反例；這不代表已取得本清單之外的授權，也不構成修改鎖檔的許可。

## P4 占位案例：24 筆

`1e24d68275b1e4225c5f1d3356b779d2407eaadf`（2026-10-21）刪除 `crates/sw-cli/tests/p4_case_placeholders.rs` 中的占位測試，commit 說明為以正式測試取代已核准的 P4 placeholder，並對齊 Linux S1A／S1B 必要集合。OD-16 的占位案例類別可涵蓋此類移除；精確 Linux 三元組未列於 OD-16／OD-17 清單，故建議補授權。

替換測試依 `tests/acceptance/p4_case_map.yaml` 的正式 case 對應列出。表內 Rust nodeid 為具體代表測試；同列 case 其餘必要測試仍以該檔為準。

| set:platform:nodeid | 移除 commit／原因 | 替換測試 | 建議 |
|---|---|---|---|
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df05_daemon_locate_order` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_daemon::start_locate_order_sibling_then_runtime`（df05） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df05_daemon_locate_order` | `1e24d68`；同上 | `sw-cli::mgmt_daemon::start_locate_order_sibling_then_runtime`（df05） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df07_bad_config_yaml` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_service::supervision_mode_bad_yaml_is_one_stderr_line_exit1`（df07） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df07_bad_config_yaml` | `1e24d68`；同上 | `sw-cli::mgmt_service::supervision_mode_bad_yaml_is_one_stderr_line_exit1`（df07） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df10_system_scope_state_path` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_setup::df10_system_state_migration_target`（df10） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df10_system_scope_state_path` | `1e24d68`；同上 | `sw-cli::mgmt_setup::df10_system_state_migration_target`（df10） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df12_setup_ran_field` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_setup::on_demand_to_systemd_user_stops_waits_then_starts`（df12） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df12_setup_ran_field` | `1e24d68`；同上 | `sw-cli::mgmt_setup::on_demand_to_systemd_user_stops_waits_then_starts`（df12） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df13_minicom_broker_rename` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_setup::materialize_wrappers_posix_only`（df13） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df13_minicom_broker_rename` | `1e24d68`；同上 | `sw-cli::mgmt_setup::materialize_wrappers_posix_only`（df13） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::df16_windows_skill_copy` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_setup::materialize_skill_always_refreshed_and_linked`（df16） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::df16_windows_skill_copy` | `1e24d68`；同上 | `sw-cli::mgmt_setup::materialize_skill_always_refreshed_and_linked`（df16） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc12_reconcile_wait_old_daemon` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_setup::reconcile_aborts_when_old_daemon_never_exits`（sc12）；`test_setup_ab_reconcile_timeout` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc12_reconcile_wait_old_daemon` | `1e24d68`；同上 | `sw-cli::mgmt_setup::reconcile_aborts_when_old_daemon_never_exits`（sc12）；`test_setup_ab_reconcile_timeout` | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc18_windows_ondemand_autostart` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_daemon::autostart_only_when_request_never_sent`（sc18）；`test_autostart_l15_tx_once` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc18_windows_ondemand_autostart` | `1e24d68`；同上 | `sw-cli::mgmt_daemon::autostart_only_when_request_never_sent`（sc18）；`test_autostart_l15_tx_once` | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc20_daemon_restart` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_daemon::restart_on_demand_stop_wait_start`（sc20）；`test_daemon_restart_ab` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc20_daemon_restart` | `1e24d68`；同上 | `sw-cli::mgmt_daemon::restart_on_demand_stop_wait_start`（sc20）；`test_daemon_restart_ab` | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc22_supervision_mode_set` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_supervision_switch::supervision_set_bad_words`（sc22） | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc22_supervision_mode_set` | `1e24d68`；同上 | `sw-cli::mgmt_supervision_switch::supervision_set_bad_words`（sc22） | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc23_setup_rollback` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_update::update_happy_path_steps_in_order`（sc23）；`test_fresh_install_blackbox` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc23_setup_rollback` | `1e24d68`；同上 | `sw-cli::mgmt_update::update_happy_path_steps_in_order`（sc23）；`test_fresh_install_blackbox` | 建議補授權 |
| `s1a_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc25_release_manifest_version_pairing` | `1e24d68`；移除 P4 占位節點，由正式 case 測試承接 | `sw-cli::mgmt_doctor::version_pairing_mismatch_detail`（sc25）；`test_doctor_ab_version_pairing_in_bundle` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::p4_case_placeholders::sc25_release_manifest_version_pairing` | `1e24d68`；同上 | `sw-cli::mgmt_doctor::version_pairing_mismatch_detail`（sc25）；`test_doctor_ab_version_pairing_in_bundle` | 建議補授權 |

## S1B 改名舊節點：2 筆

`1ea2bdccbe0a488ae79fda0157a9fcb1dc1942fd`（2026-10-21）依 `c62f606` 將斷言改為 INTERNAL_ERROR JSON 邊界測試，重新登記新名稱並維持必要案例數。OD-16 有舊名由新名替換的類別，但明列的是 S1A 兩筆；這兩筆 S1B 三元組未被 OD-16／OD-17 逐名授權，建議補授權。

| set:platform:nodeid | 移除 commit／原因 | 替換測試 | 建議 |
|---|---|---|---|
| `s1b_cargo:linux:cargo:sw-cli::mgmt_service::service_non_string_mode_date_is_typeerror_exit1_without_stdout` | `1ea2bdc`；舊名稱被新的 JSON 邊界測試名稱取代，凍結集合數量不變 | `cargo:sw-cli::mgmt_service::service_non_string_mode_date_reports_internal_error_json` | 建議補授權 |
| `s1b_cargo:linux:cargo:sw-cli::mgmt_service::service_non_string_mode_mixed_keys_is_typeerror_exit1_without_stdout` | `1ea2bdc`；同上 | `cargo:sw-cli::mgmt_service::service_non_string_mode_mixed_keys_reports_internal_error_json` | 建議補授權 |

## Windows 集合平台不適用／案例改名：10 筆

`e25aec0a5d83f661c3a389b608931071f00033df`（2026-10-20）將 POSIX facility 測試重分類為 `posix_only`，並記錄每項使用 bash、AF_UNIX 或 PTY 的理由；另依 OD-14／SC-34 更換一個已過時的錯誤碼案例。前九筆屬 Windows selector 不會執行的平台不適用案例，最後一筆有明確替換測試。這些類型與 OD-16 的平台不適用／替換案例類別相符，但 10 個 Windows 三元組未由 OD-16／OD-17 逐名核准，建議補授權。

| set:platform:nodeid | 移除 commit／原因 | 替換測試 | 建議 |
|---|---|---|---|
| `s0_win_pytest:windows:tests/test_cli_stderr_full_error.py::TestRunRpcStderrKeepsMessage::test_socket_error_stderr_includes_error_code_and_message` | `e25aec0`；POSIX AF_UNIX socket 錯誤路徑，不適用 Windows selector | 無；Windows 對應分支不等價於 AF_UNIX 測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_contracts_catalog.py::test_ec_passthrough_single_meta_entry` | `e25aec0`；OD-14／SC-34 移除 passthrough metadata 舊契約 | `tests/test_contracts_catalog.py::test_ec_no_passthrough_meta_entry` | 建議補授權 |
| `s0_win_pytest:windows:tests/test_docker_l15.py::test_longrun_and_supervision_isolated` | `e25aec0`；測試入口依賴 POSIX bash runner | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_docker_l15.py::test_longrun_refuses_before_p1` | `e25aec0`；測試入口依賴 POSIX bash runner | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_docker_l15.py::test_run_l15_dry_run_labels_report` | `e25aec0`；測試入口依賴 POSIX bash runner | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_docker_l15.py::test_run_l15_dry_run_uses_sandbox_by_id` | `e25aec0`；測試入口依賴 POSIX bash runner | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_docker_l15.py::test_run_systemd_dry_run_asserts_dropin` | `e25aec0`；測試入口依賴 POSIX bash runner | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[handoff]` | `e25aec0`；使用 POSIX PTY／`O_NOCTTY` 及 POSIX-only ttyMCU bridge | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[probe]` | `e25aec0`；同上 | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |
| `s0_win_pytest:windows:tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[race]` | `e25aec0`；同上 | 無；Linux `s0_bc` 仍收集原測試 | 建議補授權 |

## 裁決前邊界（已由 OD-23 結案）

- 上表 36 筆均待擁有者決定；不得自行移除／還原鎖項或修改 `tests/acceptance/l1_required.lock.json`。
- 若擁有者同意維持，後續變更須只經 `tests/acceptance/regen_lock.py`，逐筆採精確 `set:platform:nodeid` 核准，並保留本清單及裁決依據。
- 本盤點不改動 Stage 1／Stage 0 的 frozen denominator，也不改任何 judge 或案例斷言。
