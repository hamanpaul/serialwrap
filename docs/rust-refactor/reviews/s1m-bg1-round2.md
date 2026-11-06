# S1M-bg1 第 2 輪修正與驗證

起點：`0dc23799baf56c0226af8c621e19752c6f83be35`；分支：`feature/s1m-ba`。
程式與測試提交：`2bb93064`（`fix(stage1): 修正基線缺陷對帳與出口重判`）。
本輪只改 gate、證據保存、測試、changelog 與文件；Python daemon、RPC schema、crate 與 Cargo 設定皆未變更。

## High／Medium 處理

| finding | 結果 | 修正與回歸 |
|---|---|---|
| S1-0dc2379-1（High） | fixed `2bb93064` | Linux 四個 L1.5 producer 各讀自己的完整原始報告，依 cases／scenarios 宣告核對 repeat，全部非 PASS／非 na-only 列均須符合登記。L1 pytest／A/B producer 不再依賴 L1.5 gate id；逐步核對 resource report、JUnit 或 ab-report 的全部失敗與 gate 核准列。BB-proc 新增原始報告，Stage 1B 保存到本次證據目錄，拒絕覆蓋既有快照。 |
| S1-0dc2379-2（High） | fixed `2bb93064` | 精確對應含參數的 A/B id；有候選但不能唯一對應時拒絕豁免。只記錄精確 id，不用去參數 alias 豁免同函式其他參數。 |
| S1-0dc2379-3（Medium） | fixed `2bb93064` | slugged L1 案例依其全部失敗 A/B 證據判定。JUnit 逐列保留結果，無法解釋的 suite 計數才污染全份；未登記失敗仍擋安全不變量。基線豁免時仍對帳其他必要 passed 節點。nodeid 保留完整目錄、類別與參數，概括錯誤移除另核對 suite 計數與 A/B 一致性，涵蓋多 suite。 |
| S1-0dc2379-4（Medium） | fixed `2bb93064` | `--rejudge` 同時重判新 gate 與 rc.txt producer 步驟，輸出兩個基線分類區段、failed_steps、unresolved、suppressed。預設退出碼反映新出口；`--assert-same-judgment` 才以舊判定比對結果決定退出碼。含實際執行合成樹 gate、登記後由 FAIL 轉為不擋的回歸。 |

原始證據維持 FAIL；初始缺陷登記仍為空，沒有新增豁免。OD-30～OD-32 的既有回歸全綠；擁有者與硬體項目不改為 PASS。
OD-75 的 owner-decisions 表列在起點已存在，本輪沿用。

## 不成立主張

無。

## SPEC-GAPS

- 延續上一輪：`coverage_1a` 的 stub／e2e 消費者仍要求 class 為 `ok`／`rust-only-ok`。若登記的 daemon 缺陷同時是該 coverage 列唯一可用的證據，coverage 仍可能擋出口。本輪未放寬 coverage，也未處理未列於本輪 High／Medium 的其他 gate 規則。
- `graceful_stop.rs` 屬 `feature/s1-exit-ab-revert`，未修改。
- 依本輪最後指示，未執行實際出口證據 driver，也未重判 `s1exit-20261104T211910`；重判驗證只使用合成證據。既有出口證據的實際重判留給編排者，不宣稱出口通過。

## 測試與 gate

所有測試以前景命令執行並等待結束。測試用啟動器位於本 worktree 的 `target/s1m-verify/`，以 namespace 隔離宿主行程、production socket 與實體序列裝置，XDG 與暫存檔均指向隔離路徑；未啟動 Docker 容器，未碰實體 UART。

| 項目 | 結果 |
|---|---|
| 第一批新回歸，未修程式 | `48 failed`；另新增 producer 保存入口回歸也先確認紅燈 |
| 補強邊界，修前 | `6 failed, 50 passed`：多 suite、Stage 1B producer 不依賴 Stage 1A case、保存入口與三種快照目的地 |
| 最終受影響測試與 marker 稽核 | `379 passed, 11 warnings`；其中新增回歸 56 個，失敗 id 無 |
| 完整 `python3 -m pytest -q tests/`（一次） | `5598 passed, 127 failed, 96 errors, 139 skipped, 1 xfailed, 201 subtests passed, 14 warnings`；1491.60 秒，exit 1 |
| 上述 223 個失敗 nodeid 在 `0dc23799` 同環境重現 | `127 failed, 96 errors`；160.47 秒；逐 nodeid 與 FAILED／ERROR 分類完全一致，沒有本輪獨有項目 |
| 修正 devpts 隔離後，只補跑原失敗集合 | `198 passed, 14 failed, 7 errors, 4 skipped`；800.20 秒；skip 不算 PASS |
| 剩餘 21 個項目在起點同環境重現 | `14 failed, 7 errors`；16.54 秒；逐 nodeid 與分類完全一致 |
| 最終 `cargo test --workspace` | `1026 passed, 0 failed, 7 ignored`（85 個 test-result 摘要合計）；exit 0；ignored 不算 PASS |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0；僅交叉編譯，非 Windows 原生實跑 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m tools.contracts check` | `ok: true, findings: []` |
| `python3 tests/acceptance/regen_lock.py --add-only` | exit 0；新增集合條目 0，無鎖檔 diff |
| `python3 tests/acceptance/regen_lock.py --check --base 0dc23799` | exit 0；案例鎖、屬性快照、統計與 L1 鎖基準一致 |
| `python3 -m policy_check --repo .` | `25 pass, 0 fail, 2 warn`；R-19 與 R-22 advisory |
| `bash -n scripts/rust/stage1_exit_driver.sh`／`git diff --check` | exit 0 |

受影響測試命令：

```bash
python3 -m pytest -q tests/test_python_baseline_defects_round2.py tests/test_rust_baseline_snapshot.py tests/test_python_baseline_defects.py tests/test_rust_stage1a_gate.py tests/test_rust_stage1_exit_driver.py tests/test_rust_ab_compare.py tests/test_rust_stage1_exit_summary.py tests/test_resource_markers.py
```

早期隔離啟動器的 devpts 權限錯誤造成多項 pytest 失敗，已改為獨立 devpts 並補跑原失敗集合。Cargo 早期遇到 `file_eacces_matches_golden`（4 passed／1 failed）與 `posix_terminate_kills_whole_group_including_grandchild`（10 passed／1 failed），兩者亦在起點重現；啟動器移除 DAC 覆寫權限並讓 PID 1 回收子孫後，完整 workspace 通過。未改產品或削弱測試。

沒有受影響 crate，因此未另外跑 `--features test-fakes`；crate 與 Cargo 設定的 diff 為空。

原始 log 與完整失敗 id 保留於本機 `target/s1m-verify/`（不入版控）：`red.log`、`edge-red.log`、`final-focused.log`、`pytest-full.log`、`pytest-full.nodeids.json`、`pytest-baseline.log`、`baseline-comparison.json`、`pytest-repaired-env.log`、`pytest-repaired-env.nodeids.json`、`pytest-baseline-repaired-env.log`、`baseline-comparison-repaired-env.json`、`cargo-test-complete.log`、`gates.json` 及各 gate log。

## 既有碼待辦

修正隔離環境後仍失敗的項目如下，均已在起點重現，未修改：

- `FAILED` `tests/test_bench_tools_cli.py::test_bench_issue_post_route_local_failure_restores_old_bundle_and_benches`
- `FAILED` `tests/test_docker_upgrade.py::test_release_plan_accepts_only_real_stage1_gate_outcomes`
- `FAILED` `tests/test_p1oc_socket_perm.py::test_socket_group_chgrp_to_supplementary_group`
- `FAILED` `tests/test_rust_agent_perm_verifier.py::test_directory_target_probes_inside_directory`
- `FAILED` `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native`
- `FAILED` `tests/test_rust_bench_tools_posix.py::test_release_check_flags_populated_foreign_namespace_until_it_empties`
- `FAILED` `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`
- `FAILED` `tests/test_rust_win_scripts.py::test_runner_version_uses_crlf`
- `FAILED` `tests/test_rust_win_scripts.py::test_runner_propagates_nonzero_exit`
- `FAILED` `tests/test_rust_win_scripts.py::test_runner_forwards_serialwrap_env`
- `FAILED` `tests/test_rust_win_scripts.py::test_runner_cleans_or_keeps_staging`
- `FAILED` `tests/test_rust_win_scripts.py::test_win_test_report_schema`
- `FAILED` `tests/test_rust_win_scripts.py::test_runner_stages_fixture_dir`
- `FAILED` `tests/test_rust_win_scripts.py::test_windows_blackbox_ps1_parses_under_windows_powershell`
- `ERROR` `tests/test_rust_win_startup_100.py::test_clean_cli_is_ok_with_isolated_environment`
- `ERROR` `tests/test_rust_win_startup_100.py::test_temp_extract_dir_fails`
- `ERROR` `tests/test_rust_win_startup_100.py::test_temp_plain_file_fails_regardless_of_extension`
- `ERROR` `tests/test_rust_win_startup_100.py::test_localappdata_unpack_dir_fails`
- `ERROR` `tests/test_rust_win_startup_100.py::test_ambient_change_outside_isolation_is_recorded_not_failed`
- `ERROR` `tests/test_rust_win_startup_100.py::test_exe_dir_extract_still_fails`
- `ERROR` `tests/test_rust_win_startup_100.py::test_varying_stdout_still_fails`

其中 release 計畫的既有文字斷言仍要求「沒有 FAIL／INSUFFICIENT」，起點文件已採 OD-75 的「沒有未登記的 FAIL／INSUFFICIENT」；未透過放寬斷言放行。

## REMAINING:

none
