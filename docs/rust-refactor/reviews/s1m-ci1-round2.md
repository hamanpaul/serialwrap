# S1M-ci1 第 2 輪

起點：`8572fbbd5fb49c5b1795ef3dff5e9aba4dd352ee`；分支：`feature/s1m-ci1`。

兩項新 Medium 均 fixed `873007a3`。全套 pytest 已執行一次並完整結束，但結果不是全綠：`5868 passed, 23 failed, 7 errors, 115 skipped, 1 xfailed`。下文逐項對帳，沒有改判原始紅燈。

## High／Medium 處理

| 項目 | 處理 | 根因與修法 |
|---|---|---|
| S1-8572fbb-1／Medium | fixed `873007a3` | 沙箱探針假設 Cargo 已建立 `target/`。改以本 worktree 內的最小乾淨 checkout 執行同一份真實沙箱腳本，確認起初沒有 `target/`，並在啟動探針前建立成果父目錄。保留 namespace、空 runtime、隱藏實體 tty、根目錄唯讀與成果內容斷言。 |
| S1-8572fbb-2／Medium | fixed `873007a3` | `set -e` 使失敗的 `readlink -e` assignment 先回 exit 1，未進入證據不足分支。將 assignment 放入明確的條件判斷；解析失敗或不是 socket 均輸出診斷並回 exit 3。回歸在隱藏 `/run` 的真實 bwrap 中執行，斷言完整 stderr、exit 3、stdout 空白，確保後續命令沒有執行。 |
| 審查 A #1～#6 | already fixed（依本輪提供的非實作者審查狀態） | A 段沒有標為「未修」的項目；未重做其他分支的實作或整套 review。Low 未處理。 |

全套另定位到範圍內的 `test_rust_linux_job_ordered_commands`：首輪已改 CI，測試卻仍要求舊的直接 pytest 命令。fixed `873007a3`，改為精確要求既有 CI 的沙箱入口、`--with-docker`、resource report 與 JUnit 輸出，保留順序檢查；新增負向突變，移除沙箱入口時必須拒絕。沒有放寬成只找部分 pytest 字串。

## 原三項 CI 失敗的狀態

1. `test_bb_delegation.py` 的 WinDLL：already fixed `9a61ad82`。獨立 conftest 在 fixture 前處理 Linux 上的 `windows_native`；Windows kernel32／Job Object 案例仍保留於 Windows 凍結集合。本輪 Rust CLI 獨立 harness 自測的雙向平台防線通過，不宣稱原生 Windows 實跑。
2. `test_cli_mgmt_ab_suite.py`：namespace 修正 already fixed `9a61ad82`；沿用首輪 A／B 的 daemon restart 通過證據。完整必要集合仍需要 remote Docker 節點，未刪節點或擴張核准 skip。本輪不重跑需要 Docker 的完整 A／B。
3. `test_cli_mgmt_update.py`：runtime 可攜性與完整診斷 already fixed `9a61ad82`；原 CI 的 `INSUFFICIENT_EVIDENCE` 是否完全由 runtime 路徑造成，仍未證實。Docker 實跑待 driver／CI；合成報告自測仍會拒絕 INSUFFICIENT，不能替代實跑。

## 回歸證據

- `target/s1m-ci1-round2/regression-red.log`：修前兩項新 Medium 都轉紅，`2 failed`；分別為乾淨 checkout 的 `FileNotFoundError`，及缺少 socket 時 exit 1／stderr 空白。
- `target/s1m-ci1-round2/regression-green.log`：相同兩項修後 `2 passed`。
- `target/s1m-ci1-round2/workflow-red.log`：CI 原命令斷言與新增「拒絕移除沙箱」回歸於修前均紅，`2 failed`；後者顯示舊測試錯誤地放行直接執行黑箱的工作流程。
- `target/s1m-ci1-round2/focused-final.log`：觸及的兩份 Python 測試加 marker 稽核，`37 passed, 11 warnings`。
- `target/s1m-ci1-round2/rust-selftest.log`：以 dev bundle、`SERIALWRAP_AB_LABEL=rust-1a`、`--confcutdir=tests/contract_blackbox` 執行 harness 自測，`20 passed`。
- 兩項沙箱測試使用 `posix_only`：依賴 Linux bubblewrap、PID／mount namespace、`/proc` 與 Unix socket 路徑，原生 Windows 不適用。既有隔離探針原本即為 POSIX；新增缺少 socket 的案例也標 POSIX。CI YAML 的負向測試維持 `core_portable`／`memory_only`。沒有將既有可攜案例改標 POSIX，凍結鎖未變。

## 全套 pytest 與逐項對帳

全套前景命令為 `python3 -m pytest -q tests/`，由 `target/s1m-ci1-round2/run-check.sh` 送入 `blackbox_sandbox.sh`。真實家目錄唯讀，HOME／XDG 使用沙箱暫存目錄；沒有 Docker socket、宿主 systemd runtime 或實體序列埠。依本輪最後的明確要求執行全套，未跑第二次完整 suite。

初次 collection 因隔離 HOME 後找不到使用者安裝的 `policy_check` 而 exit 2，未執行案例；修正驗證 wrapper，保留原使用者 Python 套件的唯讀存取後重跑。紀錄為 `pytest-full-collection-error.log`。完成的全套紀錄為 `pytest-full.log`：

`5868 passed, 23 failed, 7 errors, 115 skipped, 1 xfailed, 14 warnings, 201 subtests passed`，2287.47 秒，exit 1。原始結果完整保留。

對照方法：從 `git show 8572fbbd:<path>` 保存起點檔案，在同一 worktree 的 bwrap 中以唯讀 mount 覆蓋；先要求 `git diff --exit-code 8572fbbd` 為空，再執行全套出現的 30 個 failure／error nodeid。未 reset、stash、建立其他 worktree 或切換分支。`baseline-failures.log` 結果為 `16 failed, 7 passed, 7 errors`；`failure-accounting.json` 保存逐名對帳。

### 已修或已在乾淨工作樹補驗

- `tests/test_rust_ci_workflow.py::test_rust_linux_job_ordered_commands`：在起點同樣失敗，屬本 brief 的 CI 同步缺口，fixed `873007a3`。修後整份 CI 測試及負向回歸已通過。
- 以下七項在原全套被「需要乾淨工作樹」的前置防線攔下；起點乾淨檔案對照全部通過。提交 `873007a3` 後先斷言實際工作樹乾淨，再於同樣沙箱重跑，`7 passed`（`clean-tree-followup.log`）。未修改這些測試或其前置防線。Docker 名稱的案例使用假 Docker／無效 runtime，沒有連接 Docker engine。

```text
tests/test_docker_upgrade.py::test_run_upgrade_fails_closed_without_system_python312[missing]
tests/test_docker_upgrade.py::test_run_upgrade_fails_closed_without_system_python312[not_usr_bin]
tests/test_docker_upgrade.py::test_run_upgrade_rejects_bundle_runtime_not_bound_to_usr_bin[missing_cfg]
tests/test_docker_upgrade.py::test_run_upgrade_rejects_bundle_runtime_not_bound_to_usr_bin[toolcache_home]
tests/test_docker_upgrade.py::test_run_upgrade_rejects_bundle_runtime_not_bound_to_usr_bin[usr_local_home]
tests/test_docker_upgrade.py::test_run_upgrade_rejects_bundle_runtime_not_bound_to_usr_bin[system_site_packages]
tests/test_rust_mkbundle.py::test_release_bundle_build_does_not_enable_test_fakes
```

### 起點同樣重現、保留紅燈的 22 個位置

以下每個 nodeid 均同時出現在完整 suite 與起點對照的紅燈清單。沒有將其改為 skip、PASS 或擴修其他子項。

PTY 共存五項：觀測到 tmux socket 建立錯誤，以及 vtty 在讀者握住前已被回收。起點同樣重現，沒有以「已知 flaky」放行，也沒有將主機負載當作已證實根因。

```text
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t5_bind_idempotent_preserves_console
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t6_human_sees_agent_commands
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t7_agent_runs_while_human_types
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t8_full_run_simulation
```

群組權限一項：沙箱內 socket gid 為 1000，期望附加群組為 65534；起點同樣失敗，未改 Python daemon 或權限斷言。

```text
tests/test_p1oc_socket_perm.py::test_socket_group_chgrp_to_supplementary_group
```

證據缺失兩項：缺少本 worktree 的 Windows 原生 cargo L1 報告及 P4 必要案例／L1 證據，起點同樣為證據不足。沒有跑出口 driver 補造證據；擁有者與硬體列維持 BLOCKED-OWNER，其他缺證據列仍保留 FAIL／INSUFFICIENT。

```text
tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
```

WSL interop 七項失敗：沙箱隔離後 cmd.exe／PowerShell 無法啟動，包含 `UtilGetPpid`／`UtilConnectUnix` 錯誤。未開放宿主 `/run` 或繞過安全沙箱，也未改平台 marker。

```text
tests/test_rust_win_scripts.py::test_runner_version_uses_crlf
tests/test_rust_win_scripts.py::test_runner_propagates_nonzero_exit
tests/test_rust_win_scripts.py::test_runner_forwards_serialwrap_env
tests/test_rust_win_scripts.py::test_runner_cleans_or_keeps_staging
tests/test_rust_win_scripts.py::test_win_test_report_schema
tests/test_rust_win_scripts.py::test_runner_stages_fixture_dir
tests/test_rust_win_scripts.py::test_windows_blackbox_ps1_parses_under_windows_powershell
```

同因 setup errors 七項：PowerShell 無法取得 Windows TEMP，測試本體未完成，不能計為 PASS。

```text
tests/test_rust_win_startup_100.py::test_clean_cli_is_ok_with_isolated_environment
tests/test_rust_win_startup_100.py::test_temp_extract_dir_fails
tests/test_rust_win_startup_100.py::test_temp_plain_file_fails_regardless_of_extension
tests/test_rust_win_startup_100.py::test_localappdata_unpack_dir_fails
tests/test_rust_win_startup_100.py::test_ambient_change_outside_isolation_is_recorded_not_failed
tests/test_rust_win_startup_100.py::test_exe_dir_extract_still_fails
tests/test_rust_win_startup_100.py::test_varying_stdout_still_fails
```

## 其他 gates

| 驗證 | 結果 | 本輪證據（`target/s1m-ci1-round2/` 下） |
|---|---|---|
| `cargo test --workspace` | exit 0；1041 passed、0 failed、7 ignored | `cargo-default.log`、`cargo-default-summary.json` |
| `cargo test --workspace --features test-fakes` | exit 0；1041 passed、0 failed、7 ignored | `cargo-fakes.log`、`cargo-fakes-summary.json` |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0；僅交叉編譯，非原生實跑 | `windows-check.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 | `cfg.log` |
| `python3 -m tools.contracts check` | exit 0；ok:true、findings 0 | `contracts-final.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 8572fbbd` | exit 0；案例快照、L1 結構與基準一致，沒有改鎖 | `regen-lock-final.log` |
| `python3 -m policy_check --repo .` | exit 1；24 pass、1 fail、2 warn | `policy-final.log` |
| 起點 Policy 對照 | 同為 24 pass、1 fail、2 warn；唯一 fail 為 R-07 | `policy-baseline.log` |

Policy R-07 為 `VERSION (0.4.0) mismatches latest tag (v0.3.2)`，兩個輸入在起點相同；R-19／R-22 為 warning。沒有改版本、tag 或加 exemption。Rust 的 ignored 未計為通過。

已核對完整 pytest namespace 及本輪記錄的驗證 namespace，存活行程為 0：`pytest-full-cleanup.json`、`cleanup-final.json`。未觸碰 production daemon／設定／狀態、真實序列埠、其他 worktree 或 thala 容器。未執行 Docker 容器情境、slice gate、實際出口證據 driver、push、PR、merge 或部署。

## 不成立主張

無。

## SPEC-GAPS

- 原完整 Rust CI 黑箱命令含直接 systemd Docker 與巢狀 remote Docker；本輪禁跑 Docker，不能達成同命令全部通過。update 原 runner 根因及完整 A／B 仍待有能力的 driver／CI 實跑。
- 上述 22 個已於起點重現的紅燈位置仍保留；Windows interop／原生證據、PTY 與群組權限不在本 brief 的實作範圍。沒有為全綠而開放宿主 runtime、改 Python daemon、放寬 judge 或改平台適用性。擁有者／硬體項目維持 BLOCKED-OWNER。
- 首輪 `sw-pal::fs_lock::file_lock_nonblocking_contention_wouldblock` 的紅燈是 guard drop 後未能立即重取鎖；本輪兩種 workspace Cargo 測試均未重現，但根因與歸屬仍未確證。未擴修範圍外檔案鎖，也未以綠燈抹除舊紅燈。

## 既有碼待辦

- R-07：版本／tag 對齊留給 release 流程，不在本輪改版。
- 起點已重現的環境／證據紅燈與前輪檔案鎖待分類，均如上保留；其他分支的 finding 未實作。

## Commit 與交付

- `873007a3` — `fix(ci): 補齊沙箱自測與隔離命令檢查`。
- 本報告另作文件 commit；兩個 commit 均依要求附三段 trailer。僅提交本 brief 的測試、測試工具、既有 changelog fragment 與報告，沒有修改 Rust 產品碼、Python daemon、契約、bridge 核准來源或凍結鎖。未 push。

## REMAINING:

- Docker 的 `test_update_systemd_exit1_not_relaunched_l15`、巢狀 `test_remote_topologies_docker` 與完整 CI 命令實跑；update 原 runner 根因仍待證據。
- 上列 22 個起點即重現的環境／缺證據紅燈，以及 R-07；本輪未宣稱全套或 Policy 全綠。
- 前輪檔案鎖偶發紅燈的根因與歸屬仍待分類；擁有者／硬體證據維持 BLOCKED-OWNER。
