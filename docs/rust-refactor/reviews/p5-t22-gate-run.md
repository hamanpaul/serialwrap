# P5-T22 Slice 2.1 出口 gate 非 dry-run 實跑（第三輪，2026-10-28）

- 命令：`SERIALWRAP_L15_SELFTEST_REPORT=<posix-pty selftest-report.json> bash scripts/rust/slice21_gate.sh --evidence-run --run-id s21g-261028-045014`
  （§5.3 凍結檢查未過：`event_latency.freeze_status='pending_review_record'`、審閱紀錄未附；`--evidence-run` 照跑各 section 取證，judge 恆 INCOMPLETE 以上）。
- 環境：cloud Linux（root、4 核、ext4／virtio）；`6636694`；identity：CLI sha256 `6eca14be…0885`、daemon sha256 `2ea74890…c509`（release，見 `p5-t20-blackbox-report.md`）。
- 耗時約 47 分鐘；**verdict：FAIL**（gate exit 1）。報告 schema 驗證通過。

## 各 section 判定

| section | 判定 | 數字 | 缺漏／原因 | 處置 |
|---|---|---|---|---|
| cfg_boundary | PASS | rc 0 | — | — |
| linux_l1 | FAIL | 1663 passed／16 failed／27 skipped | 10 支 sw-daemon 因測試支援以 cwd 相對路徑定位 FakeTarget 與 scope 檔，`cargo_l1_results.py` 不在 crate 目錄執行測試 binary（`ModuleNotFoundError: bb_faketarget`）；其餘 6 支為 root 下證據不足 | 已修（`tests/common/mod.rs::data_root()`，本組 commit）；同 collector 重跑兩次：8 → **6 failed**（僅 root 證據不足：`endpoint_written_after_bind`、`wal_dir_readonly_at_start_blocks_login_probe`、`wal_fault_after_ready_splits_agent_and_auto_tx`、`file_eacces_matches_golden`、`unix_fixture_rows_match_python`、`holder_scan_permission`；`setpriv --reuid=65534` 6/6 passed）。root 下仍為 FAIL，L1 收集規則屬 F108（BLOCKED-OWNER） |
| linux_cap_posix_pty | EVIDENCE_MISSING | 340／4／8 | `s21_gate:linux_cap_posix_pty` 不在 L1 鎖 | BLOCKED-OWNER（F50／F57：gate 集合登記須擁有者核可） |
| windows_native_l1 | EVIDENCE_MISSING | runner exit 127 | 無原生 Windows | LOCAL-WINDOWS-TODO（BO-S21-13） |
| windows_cap_windows_com | EVIDENCE_MISSING | — | 無 COM pair | BO-S21-01（允許的豁免） |
| blackbox_python | EVIDENCE_MISSING | 33／10／3 | 集合未登記；10 failed 見 T20 報告 | BLOCKED-OWNER（F57）＋`PA-OD-T20` |
| blackbox_rust | EVIDENCE_MISSING | 0／46 | gate 未設 Rust CLI，conftest 身分檢查 46/46 拒絕（`A/B 標籤與受測 artifact 不符`） | 已修 gate（本組 commit）；以同 artifact 的 T20 兩輪為證：36／8／2 |
| existing_suites_rust | EVIDENCE_MISSING | 4／0；func-test 3/3 | 集合未登記 | BLOCKED-OWNER（F57） |
| l15_ab | FAIL | 8 failed／35 passed（43 項） | Rust 獨有 `l15-delayed-reply`、`s21-reboot-minimal-guard`（reprobe）；兩邊皆非 PASS：`l15-wal-interference`、`s21-wal-unlink-under-traffic`、`s21-event-latency` | `PA-OD-REPROBE`、`PA-OD-L15-WAL`、`PA-OD-L15-GAPS`、`PA-OD-EVLAT` |
| event_latency | EVIDENCE_MISSING | — | 判定器未實作；兩個 daemon 皆無計時欄位 | `PA-OD-EVLAT` |
| wal_s1w_perf | FAIL | append p50 0.023／p95 0.041／p99 0.058 ms（39996 筆） | 規格 §1.2 Linux 門檻 p50≤0.005／p95≤0.01／p99≤0.02 ms：cloud virtio 未達。照實記 FAIL、保留樣本，不改列觀察值 | 待擁有者實機量測（同 BO-S21-14 的正式量測程序；Linux 實機未列 BO，建議追加） |
| case_uptake | EVIDENCE_MISSING | valid_pass 0／49 | `case_verdicts.json` 未彙整；`stage2_estimate_weeks` 未填 | 擁有者填 OD-02 輸入；逐案彙整待 T20／T21 裁決後 |
| pytest_full | EVIDENCE_MISSING | 4719 passed／27 failed／182 skipped | 集合未登記；27 failed 全數屬起點基準（無新增） | BLOCKED-OWNER（F57） |
| policy_check | PASS | fail 0 | — | — |

其餘 verdict 原因：identity 缺 `daemon_version`（`serialwrapd` 依 Python 1:1 不提供 `--version`，gate 取不到；應改由 `daemon status` RPC 取得，F139 deferred）；
parity 105 列：PASS 50、INCOMPLETE 52、OWNER-DECISION-PENDING 1、BLOCKED-OWNER 2（#6／#71 因新規則缺 decision 降為 INCOMPLETE，已補 `decision: PA-OD-1`；
PA-OD-1 已由 OD-21 核准 SC-37，但 SC-37 尚未寫入任何分支的 `contracts/behavior_changes.yaml`，故仍待登記）；§5.3 event_latency 未凍結。

## LOCAL-WINDOWS-TODO

- `windows_native_l1`：`scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_`（原生 Windows runner）。
- Windows 原生黑箱／L1.5（BO-S21-13）、Winsock 慢讀大回應、state.json CRLF、預開 port `configure_port`、overlapped 讀取失敗臂（F09）。
- `win_l1_into` 綁定本次 run（F122）。

## BLOCKED-OWNER

- BO-S21-01～16（gate 報告 `blocked_owner[]` 全列；真板依 OD-27 於 release 後、安裝機上執行）。
- gate 集合 `s21_gate:{linux_cap_posix_pty,blackbox_python,blackbox_rust,existing_suites_rust,pytest_full}` 登記（F50／F57）與 root 下 L1 收集規則（F108）。
- 待裁決：`PA-OD-REPROBE`（Slice 2.1 L1.5 與 2.2 func-test 依賴 P7-T5 reprobe）、`PA-OD-T20`、`PA-OD-L15-*`、`PA-OD-EVLAT`、`PA-OD-F67`、`NC-P5-02`、`PA-OD-1`（已核准、待登記 SC-37）。
- `event_latency` 凍結審閱紀錄（§5.3）、OD-02 `stage2_estimate_weeks`。

# 第四輪實跑（2026-10-28，run id `r4-261028`）

- 命令：`SERIALWRAP_L15_SELFTEST_REPORT=<posix-pty selftest-report.json> bash scripts/rust/slice21_gate.sh --evidence-run --run-id r4-261028`（非 dry-run）。
- 受測版本：`3c4e051a`（OD-34～OD-44／PA-OD-1 套用後、本輪 sol 增量修正前）；identity：CLI sha256 `3a5b8f52…a303a`、daemon sha256 `8bbcba2f…0fe2`（release）。
- **中斷與續跑（照實揭露）**：§1～§8 完成後 container 重啟，gate 行程遺失。§9 起以同一腳本截取的續跑副本於同一 run 目錄接續
  （未重跑已記錄段落、未改工作樹；第一次續跑漏帶 `pytest_l1` 定義於 §11 中止，再以第二份副本自 §11 續跑）；`report.json` 的
  `started_at` 因此為續跑時刻（`2026-10-28T21:32:04Z`），實際開跑約 20:53Z。
- **verdict：FAIL**（gate exit 1）。§5.3 凍結檢查未過（`event_latency.freeze_status='pending_review_record'`、審閱紀錄未附）。

| section | 判定 | 數字 | 缺漏／原因 | 處置 |
|---|---|---|---|---|
| cfg_boundary | PASS | rc 0 | — | — |
| linux_l1 | FAIL | 1709 passed／7 failed／28 skipped | 7 支皆 root 下證據不足：`daemon_process::{endpoint_written_after_bind, unwritable_state_dir_startup_save_exits_1_before_attach, wal_dir_readonly_at_start_blocks_login_probe, wal_fault_after_ready_splits_agent_and_auto_tx}`、`oserror_golden::file_eacces_matches_golden`、`rpc_client_conformance::unix_fixture_rows_match_python`、`serial::posix::tests::holder_scan_permission`；非 root（`tcargo`）重跑 `daemon_process` 32/32 passed | root 下 L1 收集規則屬 F108（BLOCKED-OWNER） |
| linux_cap_posix_pty | EVIDENCE_MISSING | 348／3／8 | 3 failed 同上 root 類；`s21_gate:linux_cap_posix_pty` 未登記於 L1 鎖 | BLOCKED-OWNER（F50／F57） |
| windows_native_l1 | EVIDENCE_MISSING | runner exit 1 | cross build 成功，`.exe` 於 Linux `Exec format error` | LOCAL-WINDOWS-TODO（BO-S21-13） |
| windows_cap_windows_com | EVIDENCE_MISSING | — | 無 COM pair | BO-S21-01（允許的豁免） |
| blackbox_python | EVIDENCE_MISSING | 40／2／6 | 集合未登記；2 failed 皆 Python 基線未出現 `SESSION_QUEUE_FULL`（`test_queue_full_backpressure`、`test_reject_paths_zero_tx[SESSION_QUEUE_FULL]`）。gate 外以同檔範圍重跑 `test_safety_core.py` 25 passed（第 255 筆回 `SESSION_QUEUE_FULL`、pending 256），根因未明，**不以 flaky 放行** | 待查（見下方「未結」） |
| blackbox_rust | EVIDENCE_MISSING | **45／0／3** | 集合未登記（第三輪 36／8／2） | BLOCKED-OWNER（F57） |
| existing_suites_rust | EVIDENCE_MISSING | 4／0；func-test 3/3 rc 0 | 集合未登記 | BLOCKED-OWNER（F57） |
| l15_ab | FAIL | 33 passed／4 failed（37 項） | 兩邊皆非 PASS：`s21-event-latency`（兩個 daemon 皆無 `test_fakes.event_latency.*` 計時欄位，INSUFFICIENT_EVIDENCE）、`s21-wal-unlink-under-traffic`（`HARNESS_STREAM_MISMATCH`，WAL 51／wire 135、131）；第三輪 8 failed。OD-34 移出的 2 案、OD-40 排除的 `l15-wal-interference` 已不在集合 | `PA-OD-EVLAT`、`PA-OD-L15-GAPS-10` |
| event_latency | EVIDENCE_MISSING | — | 判定器未實作；OD-41 停下回報 | `PA-OD-EVLAT` |
| wal_s1w_perf | FAIL | append p50 0.022／p95 0.042／p99 0.068 ms；rotate max 1067 ms（4 次 1018～1067 ms） | 規格 §1.2 Linux 門檻（p50≤0.005／p95≤0.01／p99≤0.02 ms、rotate≤3 ms）於 cloud virtio 未達；照實記 FAIL、保留樣本 | 待擁有者實機量測（同第三輪建議） |
| case_uptake | EVIDENCE_MISSING | valid_pass 0／48（OD-34 修訂後分母） | `case_verdicts.json` 未彙整 | 擁有者填 OD-02 輸入 |
| pytest_full | EVIDENCE_MISSING | 5445 passed／14 failed／227 skipped | 集合未登記；14 failed 對照起點基準（27）無新增產品回歸，詳見下方 | BLOCKED-OWNER（F57） |
| policy_check | PASS | fail 0 | — | — |

其餘 verdict 原因：identity 缺 `daemon_version`（F139 deferred）；parity 105 列：PASS 57、INCOMPLETE 46、BLOCKED-OWNER 2（第三輪 PASS 50）；pending decisions 只剩 `PA-OD-EVLAT`、`PA-OD-L15-GAPS-10`。

pytest_full 14 failed：`test_safety_core.py::test_reject_paths_zero_tx[SESSION_QUEUE_FULL]`（同上未結）；root 環境 `test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread`、`test_rust_agent_perm_verifier.py::test_directory_target_probes_inside_directory`；
鎖／擁有者：`test_l1_judge.py::{test_l1_lock_cargo_sets_exclude_ignored_tests, test_l1_lock_frozen_set_excludes_capability_gated, test_l1_lock_nodeids_subset_of_collect_only}`、`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`；
無 Windows：`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`、`test_doctor.py::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_stage0_tx_outcome.py::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit.py::{test_pyserial_chunked_write_drain_stops_after_inflight_chunk, test_pyserial_partial_cancel_keeps_cancelled_reason}`、`test_stage0_uart_exclusive.py::TestManagerAttach::test_pyserial_access_denied_raises_device_held_unknown`；
其他：`test_bench_tools_cli.py::test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches`。

## 第四輪未結

- Python 基線 `SESSION_QUEUE_FULL` 兩案於 gate 內失敗、gate 外同檔重跑通過；`0a7d0929` 上全套 pytest 同範圍重跑 13 failed（為 gate 14 failed 的子集，僅少 `test_reject_paths_zero_tx[SESSION_QUEUE_FULL]`），即同範圍兩次失敗集合不同。根因仍未明（未出現時佇列持續被接受，推測前景 `sleep` 提早結束），不以 flaky 放行，列為待查（OD-36 延伸候選）。
- gate 本身於本輪 sol 增量修正（`0a7d0929`）之前的 `3c4e051a` 執行；修正後以 cargo／全套 pytest 重驗（見 commit 說明），未重跑整個 gate。

# 第五輪實跑（2026-10-29，run id `r5b-261029`）

- 命令：`SERIALWRAP_L15_SELFTEST_REPORT=<posix-pty selftest-report.json> bash scripts/rust/slice21_gate.sh --evidence-run --run-id r5b-261029`（非 dry-run）。
- 受測版本：`464adec5`（第五輪 rulings／OD-45～OD-47 套用後）；identity：CLI sha256 `3a5b8f52…a303a`、daemon sha256 `8aae8fc3…f6cf6`（release）。
  bridge 自測：本輪重新產生（`python3 -m uart_stdio_bridge selftest --endpoint posix-pty --transport-mode virtual_pty`），`evaluate_bridge_eligibility` → `('PASS', True)`。
- **作廢的首跑 `r5-261029`（照實揭露）**：首跑期間殘留一個先前中止的基線 pytest 所留下的 Python throwaway daemon（PID 存活自 00:51），
  L1.5 兩邊 7 案皆在步驟 0 以 `ATTACH_FAILED:DeviceHeldError` 失敗、黑箱 Python／Rust 各 1 案失敗；另 event_latency 的「未凍結」被誤記為 FAIL、
  量測證據檔含本機路徑使 policy R-21 FAIL。清掉殘留 daemon、修正 gate（`464adec5`）後整包重跑即本節結果；首跑不採為證據。
- 耗時約 70 分鐘；**verdict：FAIL**（gate exit 1）。§5.3 凍結檢查未過（`event_latency` method_revision `2026-10-29.1`、`freeze_status='pending_review_record'`）。

| section | 判定 | 數字 | 缺漏／原因 | 處置 |
|---|---|---|---|---|
| cfg_boundary | PASS | rc 0 | — | — |
| linux_l1 | FAIL | 1719 passed／7 failed／28 skipped | 7 支皆 root 下證據不足（同第四輪）：`daemon_process::{endpoint_written_after_bind, unwritable_state_dir_startup_save_exits_1_before_attach, wal_dir_readonly_at_start_blocks_login_probe, wal_fault_after_ready_splits_agent_and_auto_tx}`、`oserror_golden::file_eacces_matches_golden`、`rpc_client_conformance::unix_fixture_rows_match_python`、`serial::posix::tests::holder_scan_permission`；非 root（`tcargo`，uid 1000）重跑：`daemon_process` 32/32、`oserror_golden` 5/5、`rpc_client_conformance` 7/7、`holder_scan_permission` 1/1 皆 passed | F108（BLOCKED-OWNER） |
| linux_cap_posix_pty | EVIDENCE_MISSING | 347／4／8 | 集合未登記 | BLOCKED-OWNER（F50／F57） |
| windows_native_l1 | EVIDENCE_MISSING | runner exit 1 | cross build 成功；`.exe` 無法於 Linux 執行 | LOCAL-WINDOWS-TODO（BO-S21-13） |
| windows_cap_windows_com | EVIDENCE_MISSING | — | 無 COM pair | BO-S21-01 |
| blackbox_python | EVIDENCE_MISSING | **43／0／6** | 集合未登記；第四輪的 `SESSION_QUEUE_FULL` 兩案本輪通過（見下方「第四輪未結」更新） | BLOCKED-OWNER（F57） |
| blackbox_rust | EVIDENCE_MISSING | **46／0／3** | 集合未登記（含 OD-46 新案 `test_wal_unlink_under_sustained_traffic_keeps_evidence` passed） | BLOCKED-OWNER（F57） |
| existing_suites_rust | EVIDENCE_MISSING | 4／0；func-test 3/3 rc 0 | 集合未登記 | BLOCKED-OWNER（F57） |
| l15_ab | FAIL | 33 passed／2 failed（35 項） | 只剩 `s21-event-latency` 兩邊（以 daemon 計時欄位量測，OD-45 下恆 INSUFFICIENT_EVIDENCE）。`s21-wal-unlink-under-traffic` 依 OD-46 移出（第四輪 4 failed） | OWNER-BATCH（建議比照 OD-40／46 移出） |
| event_latency | EVIDENCE_MISSING | Rust 18 輪全 PASS；L1 gate 2 支 passed | 唯一原因：上限值未重新凍結（`pending_review_record`）。Rust 每輪 max：stop 0.96～3.93 ms、cancel 1.41～8.23 ms、release 5.47～38.36 ms（上限 idle 50／flood 100）。Python 診斷（50 樣本）：idle release_request 三輪 153～155 ms（> 50），其餘 PASS | OWNER-BATCH（獨立審閱＋重新凍結） |
| wal_s1w_perf | FAIL | append p50 0.028／p95 0.041／p99 0.068 ms；rotate max 378 ms | 規格 §1.2 Linux 門檻於 cloud virtio 未達（同前兩輪）；照實記 FAIL | 待擁有者實機量測 |
| case_uptake | EVIDENCE_MISSING | — | `case_verdicts.json` 未彙整；`stage2_estimate_weeks` 未填 | 擁有者填 OD-02 輸入 |
| pytest_full | EVIDENCE_MISSING | 5454 passed／13 failed／226 skipped | 集合未登記；13 failed 全數屬起點基準（`cca5ba8`，乾淨 worktree，24 failed）之子集，無新增 | BLOCKED-OWNER（F57） |
| policy_check | PASS | fail 0 | — | — |

其餘 verdict 原因：identity 缺 `daemon_version`（F139 deferred）；parity 105 列：PASS 57、INCOMPLETE 46、BLOCKED-OWNER 2（同第四輪）；pending decisions 已清空（OD-45～OD-47）。

pytest_full 13 failed：root 環境 `test_bb_l15_holder_observer.py::test_observer_does_not_ignore_zombie_leader_with_live_thread`、`test_rust_agent_perm_verifier.py::test_directory_target_probes_inside_directory`、`test_bench_tools_cli.py::test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches`；
鎖／擁有者：`test_l1_judge.py::{test_l1_lock_cargo_sets_exclude_ignored_tests, test_l1_lock_frozen_set_excludes_capability_gated, test_l1_lock_nodeids_subset_of_collect_only}`、`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`；
無 Windows／pyserial：`test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`、`test_doctor.py::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_stage0_tx_outcome.py::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit.py::{test_pyserial_chunked_write_drain_stops_after_inflight_chunk, test_pyserial_partial_cancel_keeps_cancelled_reason}`、`test_stage0_uart_exclusive.py::TestManagerAttach::test_pyserial_access_denied_raises_device_held_unknown`。

起點基準（`cca5ba8`，`/tmp` 下乾淨 worktree，python3.12，root）：24 failed／5436 passed／227 skipped。比 gate 多出的 11 支：9 支 `test_rust_no_onefile_private_tmp.py`（worktree 位於 `/tmp`，`--private-tmp` namespace 內看不到工作目錄，位置性）、`test_rust_goldens_fresh.py::test_golden_is_fresh[gen_allowlist_unreadable_config.py-…]`、`test_safety_core.py::test_reject_paths_zero_tx[CMD_TOO_LONG]`；基準期間同樣有上述殘留 daemon，僅作「不得超出」的上界使用。

## 第四輪未結（更新）

- Python 基線 `SESSION_QUEUE_FULL` 兩案本輪 gate 內通過（blackbox_python 與 pytest_full 皆然）。首跑 `r5-261029` 時殘留 throwaway daemon 使相關案例失敗，推測第四輪的不穩定可能同源（同機殘留 daemon／PTY holder），未能證實；維持觀察，不以 flaky 放行。

## 第五輪 event_latency 證據重採（sol P5-3084388-2）

- gate 內的 `event_latency/rust.json` 由 `464adec` 版工具產生，缺受測身分；`2472f78` 補上 identity 後，以乾淨工作樹、gate 同一 release artifact
  （daemon sha256 `8aae8fc3…f6cf6`）重採三輪，存為 `p5-evidence/event-latency-od45-rust-evlat-r5-2472f78.json`：18 輪全 PASS、L1 gate 2 支 passed，
  verdict INSUFFICIENT_EVIDENCE 的唯一原因為上限值未重新凍結。
