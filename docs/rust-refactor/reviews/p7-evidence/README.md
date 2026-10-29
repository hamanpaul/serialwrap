# P7 雲端 session 執行證據（2026-10-27）

審查 de24500_fa847c7（Opus #9、grok #5）要求可追溯的 gate 證據。以下為本 session 在雲端容器（Linux、root、
CPython 3.12.3 跑 pytest；系統 `python3` 為 3.11.17）實跑的失敗清單（nodeid 排序去重）。

| 檔案 | 時點 | 筆數 | sha256 前 16 |
|---|---|---|---|
| `pytest-fail-start-de24500.txt` | 起點 de24500（`python3.12 -m pytest -q tests/` 分段前景） | 543 | 2ed0d942635a3c54 |
| `pytest-fail-merge-be89081.txt` | merge P5 T15～T19 後 | 509 | de1e51bc8ae12610 |
| `pytest-fail-92e0223.txt` | Task 6 後 | 505 | 97227e891849425d |
| `pytest-fail-d5b2dd6.txt` | 審查第 3 輪後（多出 `test_functest_bridge_smoke::test_bridge_smoke_sm04_until_op`：Python daemon `ATTACH_FAILED:DeviceHeldError`、本 commit 未改 `sw_core/`，同 sm01 環境類；`test_multiagent_e2e` 五 agent 案重跑 2/2 通過） | 508 | c2e8c596adb453e6 |
| `pytest-fail-c58e8d4.txt` | Task 13／13-py 後（多出者：`test_l15_smoke::test_reject_zero_tx_runs_on_python`、`test_functest_bridge_smoke::*sm01*` 為 Python daemon `ATTACH_FAILED:DeviceHeldError`，於 76de0cf worktree 同樣失敗；容器內殘留一個早先 slice21 測試洩漏的 throwaway `serialwrapd`（auto mode 不允許本 session 終止），疑為成因；`test_functest_windows_wiring::test_windows_wiring_env_and_argv` 單獨執行於 76de0cf 亦失敗（`WindowsPath` 於 POSIX 不可實例化，順序相依）；`test_human_agent_coexist` t1 單獨 2/2 通過；`test_regression_windows_caps` 單獨 2/2 通過） | 511 | ba8d5196ca29c0e3 |
| `pytest-fail-91109e7.txt` | Task 14＋15 後（相對 c58e8d4 無新增失敗） | 508 | 5dca7ec693660c98 |
| `pytest-fail-ae82afe.txt` | 審查第 4 輪後（相對 91109e7：+`test_mgmt_stubs::test_live_daemon_fixture_serves_attached_console_list`（ERROR，單獨重跑 2/2 通過）、+`test_safety_core[SESSION_NOT_READY-ATTACHING]`（起點清單內）） | 510 | a12acbf50459427d |
| `pytest-fail-aaf2c1d.txt` | 審查第 5 輪後（相對 ae82afe：+1／−3——+`test_bb_daemon_e2e::test_session_reaches_ready`（`逾時 20.0s；最後觀察值：None`；同範圍兩次重跑 1 failed／1 passed，集合不同） | 508 | 031452ef72aa78c7 |
| `pytest-fail-9c4a80e.txt` | 審查第 6 輪後（相對 aaf2c1d：+1／−1——−`test_bb_daemon_e2e::test_session_reaches_ready`（上一輪已記為同範圍兩次重跑、集合不同）；+`test_l15_smoke::test_reject_zero_tx_runs_on_python`——`DeviceHeldError` 環境類，c58e8d4 列已於 76de0cf worktree 重現） | 508 | 5dca7ec693660c98 |
| `pytest-fail-0722f75.txt` | Task 16＋16-wire 後（相對 9c4a80e：+5／−1——−sm01；+slice21 `test_f1_cmd_contract::test_limits_queryable`／`test_newline_rejected`、`test_f12_version_reported_consistent`（ERROR；單獨重跑首次為 Python daemon `ATTACH_FAILED:DeviceHeldError`，合併重跑 4 passed）、+`test_mgmt_stubs` live daemon fixture（同上 4 passed）、+sm04（DeviceHeldError 環境類）） | 512 | f1fcb15f24ef4376 |
| `pytest-fail-3030adc.txt` | 審查第 7 輪後（相對 0722f75：+2／−3——+`tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`；+`tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`；−`tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_limits_queryable`；−`tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_newline_rejected`；−`tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`；新增兩筆分別在起點清單內（SESSION_NOT_READY-ATTACHING）與 DeviceHeldError 環境類（sm01）） | 511 | 5b1216e4c3e40aac |
| `pytest-fail-start-a0cad06.txt` | 第 3 輪 session 起點 a0cad06（`git worktree` 於 `/tmp`，`python3.12 -m pytest -q -p no:cacheprovider tests/ -rfE`；73 failed／432 errors） | 505 | 2b8fe0b8f0458c21 |
| `pytest-fail-96c9f02.txt` | merge P5（a089f9f）＋第 8 輪審查修正＋Task 10／9／17 後（同上命令；45 failed／432 errors）。相對起點 +1／−29：−28 為 P5 交付的 slice21 calibration／gate judge／gate report 與 `SESSION_NOT_READY-ATTACHING`；+1 為 `test_rust_goldens_fresh::test_golden_is_fresh[gen_allowlist_unreadable_config.py-allowlist_unreadable_config.tsv]`（P5 F84 帶入的 golden；主工作樹同範圍重跑 2/2 passed，`origin/feature/rust-refactor-p5` 的 `/tmp` worktree 重跑 2/2 failed。成因未實證（審查 a0cad06_4221ba2 4221ba2-1 更正）：疑似 `/tmp/<0700 目錄>` 使 generator 降權列無法進入父目錄、例外類別改變，與路徑字串無關；第 4 輪主工作樹全套未再出現；P5 範圍） | 477 | 5130dd36be4b5478 |
| `pytest-fail-start-4221ba2.txt` | 第 4 輪 session 起點 4221ba2（主工作樹；`python3` 改指 CPython 3.12.3 並裝 pytest／PyYAML／jsonschema，故 `test_bb_cli_help` 432 筆 ERROR 不再出現；`python3 -m pytest -q -p no:cacheprovider -rfE` 依檔名 8 分段前景執行） | 28 | 666a6afab09553d1 |
| `pytest-fail-f150a24.txt` | merge 主線（9d21ef7）＋P5（b0772fe）＋第 9 輪修正（f150a24）＋P7 golden 重擷（#234 `boot_done_regex` 欄）後（同上命令；`test_bb_daemon_e2e.py` 因主線新增 27 案、每案 setup 10.7 s 另段執行）。相對起點 +2／−6，見下段 | 24 | 199751778b9fa817 |
| `pytest-fail-7e2da7a.txt` | 第 9 輪 Opus 修正（bf26e33）＋#234（7e2da7a）後（同上命令；改依檔名 10 分段＋`test_bb_daemon_e2e.py` 獨立一段）。相對起點同 f150a24 列 +2（holder observer zombie 環境類、OD-24 防線 P7-BO-4）；相對 f150a24 多 `test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`（起點清單內、歷次重跑集合不同） | 25 | 7f30fe87842ba86f |
| `pytest-fail-cf2ba94.txt` | Task 7 SC-30（4f0754b）＋OD-34／31 gate 測試（cf2ba94）＋overflow 管線修正（工作樹）後（10 分段＋e2e 段）。相對起點 +2（同 f150a24 列），其餘皆在起點清單內；新增的 `tests/acceptance/l15/test_slice23_l15.py` 於全套中因未設 Rust daemon 環境變數而 skip | 24 | 199751778b9fa817 |
| `pytest-fail-279ab27.txt` | log_status 單一快照（20c9454）＋SC-30 黑箱（279ab27）後（10 分段＋e2e 段）。相對起點 +2（同 f150a24 列）；新增黑箱於 python label 為 Rust-only skip | 25 | 7f30fe87842ba86f |
| `pytest-fail-c170df7.txt` | Task 17 剩餘列與 attach 形狀修正（c170df7）後（10 分段＋e2e 段）。相對起點 +2（同 f150a24 列） | 24 | 199751778b9fa817 |
| `pytest-fail-1677489.txt` | login_finished 世代核對（1677489）後（10 分段＋e2e 段；e2e 段因 worker 重啟重跑一次，39 passed）。相對起點 +2（同 f150a24 列） | 22 | f340048e07bc80b5 |
| `cargo-fail-start-de24500.txt` | 起點 `cargo test --workspace --features test-fakes --no-fail-fast` | 51 | f357f5d2317730d8 |

失敗主要成因（非本分支引入，皆於起點即存在或由 P5 merge 帶入、在 P5 分支同樣失敗）：
- `tests/contract_blackbox/test_bb_cli_help.py` 432 筆 ERROR：fixture 以系統 `python3`（3.11.17）比對 golden 方言 3.12.3，
  `AssertionError: Python 基線 3.11.17 ≠ golden 方言 3.12.3（證據不足）`——容器環境差異。
- `tests/test_slice21_calibration.py`／`test_slice21_gate_judge.py`／`test_slice21_gate_report.py`：P5-T22／T23 的
  `scripts/rust/slice21_gate_judge.py` 與 `docs/rust-refactor/slice21-calibration.md` 尚未交付（P5 範圍）。
- `tests/test_rust_mkbundle.py`、`test_rust_bench_tools.py` 等：需原生 Windows／打包工具。
- `tests/contract_blackbox/slice21/*` 11～12 筆：P5 預寫黑箱案例（P5 分支同樣失敗）；setup 偶發
  `ATTACH_FAILED:DeviceHeldError`（同範圍重跑兩次失敗集合不同）。
- cargo：起點 51 筆中 46 筆為 P5-T15～T19 骨架（`not implemented: slice21 T15`），merge 後只剩 sw-pal
  `file_eacces_matches_golden`、`unix_fixture_rows_match_python` 兩筆（root 執行使 EACCES 不成立的環境差異）。

每個 commit 群組 push 前的判定：失敗集合不得超出起點（多出者同範圍重跑兩次取證）；比對結果記於各 commit message。

### d5b2dd6 列的完整差異（審查 92e0223_76de0cf 76de0cf-1 補記）

相對 `pytest-fail-92e0223.txt`：**+4／−1**。

- `+ tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`（ERROR）
- `+ tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
- `+ tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`
- `+ tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`
- `− tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`

同範圍連跑兩次（CLAUDE.md 要求「同範圍連跑兩次、失敗集合不同」）：

```
$ timeout 280 python3.12 -m pytest -q -p no:cacheprovider tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op \
    tests/test_l15_smoke.py::test_release_inflight_runs_on_python "tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict"
第 1 次：1 failed, 2 passed（失敗者為 sm04：Python daemon `ATTACH_FAILED:DeviceHeldError`；multiagent 五 agent 案通過）
第 2 次：1 failed, 2 passed（同上）
$ timeout 290 python3.12 -m pytest -q -p no:cacheprovider tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent \
    "tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]"
第 1 次：1 failed, 1 error
第 2 次：1 failed, 1 passed（失敗集合不同）
```

multiagent 五 agent 案在重跑中 2/2 通過、全套中失敗一次，前兩筆 slice21 在起點清單內且兩次重跑集合不同；sm04 與 sm01 同為
`DeviceHeldError` 環境類（c58e8d4 列另以 76de0cf worktree 重現）。d5b2dd6 未改 `sw_core/`。

### c58e8d4 列的完整差異（審查 76de0cf_96b28dd 96b28dd-1 補記）

相對 `pytest-fail-d5b2dd6.txt`：**+7／−4**。

- `+ tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]`
- `+ tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
- `+ tests/test_functest_windows_wiring.py::test_windows_wiring_env_and_argv`
- `+ tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console`
- `+ tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python`
- `+ tests/test_regression_windows_caps.py::test_regression_case_requires_updated`
- `+ tests/test_regression_windows_caps.py::test_regression_windows_skip_is_capability_based`
- `− tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`
- `− tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`
- `− tests/test_l15_smoke.py::test_release_inflight_runs_on_python`
- `− tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`

同範圍重跑（本 session 實跑）：

```
$ timeout 290 python3.12 -m pytest -q -p no:cacheprovider tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console \
    tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python "tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]" tests/test_regression_windows_caps.py
第 1 次：2 failed, 11 passed
第 2 次：1 failed, 12 passed（失敗集合不同；-rf 顯示唯一失敗者為 l15 reject_zero_tx：Python daemon `ATTACH_FAILED:DeviceHeldError`）
$ timeout 200 python3.12 -m pytest -q -p no:cacheprovider tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console
第 1 次：1 passed；第 2 次：1 passed（全套中失敗一次，同範圍兩次皆過 → 失敗集合不同）
$ timeout 280 python3.12 -m pytest -q -p no:cacheprovider tests/test_regression_windows_caps.py tests/test_functest_windows_wiring.py::test_windows_wiring_env_and_argv
第 1 次：1 failed, 10 passed；第 2 次：1 failed, 10 passed（regression_windows_caps 兩次皆過；windows_wiring 單獨執行恆紅，
於 76de0cf worktree 同樣恆紅：`NotImplementedError: cannot instantiate 'WindowsPath' on your system`，全套中依執行順序時過時不過）
$ （76de0cf worktree）timeout 200 python3.12 -m pytest -q -p no:cacheprovider tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python
1 failed（ATTACH_FAILED:DeviceHeldError）——與 c58e8d4 相同，非本範圍引入
```

殘留 throwaway daemon 的觀察（本 session，2026-10-27 約 12:35 UTC）：`ps` 見 pid 11089
`python3 …/tests/contract_blackbox/bin/../../../serialwrapd.py`（etime 05:23:28），`/proc/11089/environ` 的
`SERIALWRAP_STATE_DIR=/tmp/pytest-of-root/pytest-40/test_reject_paths_zero_tx_SESS0/shell/state`、
`SERIALWRAP_RUN_DIR=/tmp/sw21-_65e8la_`，持有 `/dev/ptmx`×2 與該測試的 WAL handle。auto mode 不允許本 session 終止該行程，
故 DeviceHeldError 類失敗與其關聯僅為推測；審查者容器中無此行程、sm01 通過，與此推測一致。

### 逐列完整 +/−（自動產生；審查 06c61d0_b65df2d b65df2d-1、b65df2d_01c474c 01c474c-1）

以 `comm` 比對相鄰兩個清單檔（`+` 為新增失敗、`−` 為不再失敗）。各新增項的成因判定見上表與前述各段。

- `start-de24500` → `merge-be89081`：+7／−41
  - `+ FAILED tests/contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`
  - `+ FAILED tests/contract_blackbox/slice21/test_f2_backpressure.py::test_recovery_flushes_queue`
  - `+ FAILED tests/contract_blackbox/slice21/test_f3_observability.py::test_device_error_names_selector`
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_recovering_submit_session_recovering`
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_stale_context_not_replayed`
  - `+ FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
  - `+ FAILED tests/test_l15_smoke.py::test_release_inflight_runs_on_python`
  - `− ERROR tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_wal_path_live_marker_crc`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_cmdid_survives_timeout`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_near_limit_no_logout`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_too_long_rejected`
  - `− ERROR tests/contract_blackbox/slice21/test_f2_backpressure.py::test_history_bounded`
  - `− ERROR tests/contract_blackbox/slice21/test_f2_backpressure.py::test_queue_full_backpressure`
  - `− ERROR tests/contract_blackbox/slice21/test_f2_backpressure.py::test_recovery_flushes_queue`
  - `− ERROR tests/contract_blackbox/slice21/test_f2_backpressure.py::test_rx_window_crossing_prompt`
  - `− ERROR tests/contract_blackbox/slice21/test_f3_observability.py::test_cmd_fail_observable`
  - `− ERROR tests/contract_blackbox/slice21/test_f3_observability.py::test_device_error_names_selector`
  - `− ERROR tests/contract_blackbox/slice21/test_f3_observability.py::test_fail_error_code`
  - `− ERROR tests/contract_blackbox/slice21/test_f3_observability.py::test_log_tail_latest`
  - `− ERROR tests/contract_blackbox/slice21/test_f4_session_semantics.py::test_activity_classification`
  - `− ERROR tests/contract_blackbox/slice21/test_f4_session_semantics.py::test_background_result_tail_consistent`
  - `− ERROR tests/contract_blackbox/slice21/test_f4_session_semantics.py::test_interactive_line_cmd_defined`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_attach_not_ready_last_error_field`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_credentials_unresolved_no_empty_login`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_external_open_blocked_then_release_attach_handoff`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_passthrough_quiet_ready_and_cmd`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_recovering_submit_session_recovering`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_released_survives_two_restarts_dynamic`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_socket_mode_0660`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_stale_context_not_replayed`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_two_daemons_same_tty`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_wal_unlink_recreates_with_meta`
  - `− ERROR tests/contract_blackbox/slice21/test_safety_core.py::test_zero_tx_while_autoboot_quiet`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[CMD_CONTAINS_NEWLINE]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[CMD_TOO_LONG]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[PROFILE_NOT_COMMAND_CAPABLE]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-DETACHED]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-PASSTHROUGH]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-RELEASED]`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_boot_banner_precedes_first_prompt[login-false-shell-prompt]`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_boot_banner_precedes_first_prompt[login-true-login-prompt]`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_login_false_sends_prompt_at_boot`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_sleep_command_sends_prompt_after_n_seconds`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_written_returns_all_bytes_received_by_dut`
  - `− FAILED tests/contract_blackbox/test_bb_faketarget_s21.py::test_yes_head_emits_exact_bytes_then_prompt`
  - `− FAILED tests/test_p7_skeleton_boundary.py::test_p7_files_exist_and_cfg_boundary_clean`
  - `− FAILED tests/test_rust_cfg_boundary.py::test_repo_clean`
- `merge-be89081` → `92e0223`：+0／−4
  - `− ERROR tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_limits_queryable`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_newline_rejected`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
- `92e0223` → `d5b2dd6`：+4／−1
  - `+ ERROR tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
  - `+ FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`
  - `+ FAILED tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`
  - `− FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
- `d5b2dd6` → `c58e8d4`：+7／−4
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]`
  - `+ FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
  - `+ FAILED tests/test_functest_windows_wiring.py::test_windows_wiring_env_and_argv`
  - `+ FAILED tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console`
  - `+ FAILED tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python`
  - `+ FAILED tests/test_regression_windows_caps.py::test_regression_case_requires_updated`
  - `+ FAILED tests/test_regression_windows_caps.py::test_regression_windows_skip_is_capability_based`
  - `− ERROR tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`
  - `− FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`
  - `− FAILED tests/test_l15_smoke.py::test_release_inflight_runs_on_python`
  - `− FAILED tests/test_multiagent_e2e.py::TestMultiAgentE2E::test_five_agents_three_rounds_no_conflict`
- `c58e8d4` → `91109e7`：+0／−3
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[INVALID_COMPLETION_REGEX]`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
  - `− FAILED tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console`
- `91109e7` → `ae82afe`：+2／−0
  - `+ ERROR tests/contract_blackbox/test_mgmt_stubs.py::test_live_daemon_fixture_serves_attached_console_list`
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
- `ae82afe` → `aaf2c1d`：+1／−3
  - `+ FAILED tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready`
  - `− ERROR tests/contract_blackbox/test_mgmt_stubs.py::test_live_daemon_fixture_serves_attached_console_list`
  - `− FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
  - `− FAILED tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python`
- `aaf2c1d` → `9c4a80e`：+1／−1
  - `+ FAILED tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python`
  - `− FAILED tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready`
- `9c4a80e` → `0722f75`：+5／−1
  - `+ ERROR tests/contract_blackbox/slice21/test_f12_diagnostics.py::test_f12_version_reported_consistent`
  - `+ ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_limits_queryable`
  - `+ ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_newline_rejected`
  - `+ ERROR tests/contract_blackbox/test_mgmt_stubs.py::test_live_daemon_fixture_serves_attached_console_list`
  - `+ FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`
  - `− FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
- `0722f75` → `3030adc`：+2／−3
  - `+ FAILED tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`
  - `+ FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_limits_queryable`
  - `− ERROR tests/contract_blackbox/slice21/test_f1_cmd_contract.py::test_newline_rejected`
  - `− FAILED tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm04_until_op`

### 第 8 輪補記：sm01／SESSION_NOT_READY-ATTACHING 同範圍重跑（審查 01c474c_a0cad06 a0cad06-1）

命令：`python3.12 -m pytest -q -p no:cacheprovider tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01 "tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]"`（主工作樹 96c9f02，連跑 3 次）：
`2 passed`／`1 failed, 1 passed`／`2 passed`——同範圍失敗集合不同，與既有「DeviceHeldError 環境類」判定一致；兩者皆不在 `pytest-fail-96c9f02.txt`。

### f150a24 列的完整差異（第 4 輪）

相對 `pytest-fail-start-4221ba2.txt`：**+2／−6**。

- `+ tests/test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread`：主線 4ad9b39 新增，
  期望 `PermissionError`；root 執行讀得到 zombie leader 的 fd 目錄（`DID NOT RAISE PermissionError`）。於
  `origin/feature/rust-refactor` worktree 同範圍連跑兩次皆 `1 failed`（環境類，非本分支引入）。
- `+ tests/test_l1_judge.py::test_l1_lock_cargo_sets_exclude_ignored_tests`：主線 OD-24 防線對 P5 `s21_core` 的
  `#[ignore]` 列報錯（linux 27、windows 13），移除須擁有者逐名授權（BLOCKED-OWNER P7-BO-4）。
- `− tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`、
  `− tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_suite_execution`、
  `− tests/contract_blackbox/test_cli_mgmt_l15.py::{test_reject_conflict_l15_zero_tx,test_restart_no_reopen_before_handoff,test_restart_no_stale_console_replay}`、
  `− tests/test_functest_windows_wiring.py::test_windows_wiring_env_and_argv`（主線帶入的修正／順序相依）。
- 過程中另有 `tests/test_p7_goldens_fresh.py::test_goldens_exist_and_are_fresh`：主線 #234 使 Python `ProfileTemplate`
  多出 `boot_done_regex` 欄，`auth_resolution`／`login_fsm_scenarios`／`quiet_rx` 三檔以 `capture_p7_goldens.py` 重擷；
  JSON 比對去掉該鍵後 113 列內容完全相同。重擷後 6 passed，不列入清單。

cargo（`--workspace --features test-fakes`）失敗 6 筆皆為 root 使 chmod 無效的環境類（`daemon_process` 3、sw-pal
`holder_scan_permission`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python`），訊息皆為「證據不足：root 下 chmod
無效…」。

### 第 11 輪（第五輪雲端，ubuntu 非 root 執行）

- 起點 `9d35b01`：`pytest-fail-start-9d35b01.txt`（5464 passed、21 failed；slice21 黑箱 10 筆為與 g1 並行時的負載／競態，
  g1、g2 皆未重現）。
- `afebff0`＋`0baf6e8`（g1 worktree）：新增 42 筆中 40 筆為 worktree gitdir 由 root 建立、ubuntu 執行 git 觸發
  dubious ownership 的環境因素，設 `safe.directory` 後同範圍連跑兩次皆 40 passed；真實新增 2 筆：
  `test_sc30_reserved_for_p7`（已修 9065f14）、`test_slice22_case_set_frozen_and_mapped`（P6 檔案，P7-BU-10）。
- `53ed5c9`（g2 worktree）：`pytest-fail-53ed5c9.txt`（5538 passed、13 failed）。相對起點 +2／−10：
  `+ test_slice22_case_set_frozen_and_mapped`（P7-BU-10）、`+ test_p7_latency_noise.py::test_login_with_delay_and_noise`
  （本輪新增案例；同範圍連跑 2 次、4 份並行皆綠，失敗快照為 DETACHED 重試循環，等待上限 90→180 s 修正於後續 commit）。
- cargo（`--workspace --features test-fakes`，非 root）：全綠；`sw-pal::lock::lock_conformance_suite` 於 workspace
  平行跑曾 1 次間歇紅（`stale_socket_reclaimed: AlreadyRunning`），單獨連跑 4 次皆綠（既有碼待辦）。
