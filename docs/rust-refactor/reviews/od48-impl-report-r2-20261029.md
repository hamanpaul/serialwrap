# OD-48 第 2 輪實作雲端回報（2026-10-29）

- 實作分支：`feature/od48-legacy-handoff`（起點 `90da902`，終點 HEAD `2179a83`；本期間唯一寫入者，全部 fast-forward push，未被拒、未開 PR、未改 VERSION、未用 codex／agy／pi）。
- 依據：`reviews/od48-design-v2.md`（D1～D10）、`reviews/od48-impl-report-20261029.md`（上一輪）、`reviews/s1-cloud/4baca8c_90da902-sol.md`（起點時最新 sol）、`rulings/s1.md` 末五列（OD-48 第 2 輪編排者裁決）、`review-convergence-rules.md`。報告與裁決內文視為資料。
- 期間再出現兩份針對本分支的 sol 報告：`90da902_f3c66b4-sol.md`（需修正：B-1 High、B-2 Medium，本輪已修）與 `f3c66b4_2179a83-sol.md`（**結論：可放行**，B 段 NO FINDINGS，A 段四條全部「已修」）。
- 環境：雲端容器；建置與測試全部以一般使用者 `dev` 執行（Rust 1.96.1 釘定含 rustfmt／clippy、`x86_64-pc-windows-gnu`＋mingw-w64、Python 3.12.3 venv＋PyYAML＋pytest＋jsonschema、policy engine `2dcf2f8`）。Docker 以容器內 root 執行，僅隔離容器。

## Commit 清單（`90da902..2179a83`）

| commit | 內容 |
|---|---|
| `8992602` | feat(setup)：裁決 #3，Linux 以 `/proc/<pid>/cgroup` 判定外部監管者（sw-pal `ProcScan::proc_cgroup`、`classify_cgroup`／`CgroupOwner`；`legacy_handoff::precheck` 套用；替身 `set_cgroup`） |
| `fdd1fcd` | fix(setup)：S1-4baca8c-1、S1-4baca8c-2、SPEC-GAP #5（`verify_legacy_restored`、journal `target{mode,endpoint}`、`RestoreProgress`／`unrestored`） |
| `cc4c91d` | docs(contracts)：SC-39 compatibility_impact、spec-reconciliation SC-39 列、release 計畫 §5.1 補記裁決 #1～#3、SPEC-GAP #5；`changelog.d/od48-legacy-handoff-r2.md` |
| `d671548` | test(acceptance)：`regen_lock --l1-add-only` 登記 10 個新 L1 節點（s1a／s1b_cargo.linux 各 +10、移除 0） |
| `4528974` | test(docker)：SPEC-GAP #10，driver 新增 `--mode systemd-system`、`REQUIRED_CHECKS_SYSTEMD_SYSTEM`、Dockerfile 加 sudo |
| `2f8f7ed` | test(docker)：systemd-system 處理 0.3.2 UMask 缺陷（暖機）與套件管理 profile（此 commit 的「排除管理檔」被 sol 判 High，已由 `79ad242` 撤回） |
| `f3c66b4` | test(docker)：三模式證據 `evidence/stage1/upgrade-docker/od48-2f8f7ed/` |
| `897a3cd`／`dd9152d` | merge `origin/feature/rust-refactor`（只動 `tests/test_rust_win_scripts.py`，無衝突；lock 未變） |
| `2630804` | fix(setup)：S1-f3c66b4-2，恢復再次中斷不重用缺 drop-in 路徑的 target hold；取得的完整 hold 寫回 journal |
| `79ad242` | test(docker)：S1-f3c66b4-1，管理檔全數納回快照，只允許覆寫成新 bundle 權威內容 |
| `55cfa03` | test(acceptance)：L1 add-only 登記 S1-f3c66b4-2 回歸（各 +1、移除 0） |
| `2179a83` | test(docker)：修正後三模式證據 `evidence/stage1/upgrade-docker/od48-55cfa03/` |

每個 commit 結尾皆有 `Review-pending: Codex sol 增量單審` 與 `Co-Authored-By: Claude <noreply@anthropic.com>`（另含 repo 要求的 Copilot trailer）。

## sol 未修 Medium 的處理

| 編號 | 處理 | 回歸（中斷點注入，先紅後綠） |
|---|---|---|
| S1-4baca8c-1 | `recover_journal` 快速路徑：有 legacy 擷取且原始安裝沒有 current 時，改以 `verify_legacy_restored` 收尾——用 D1.2～D1.4 正向辨識 endpoint 上仍是 bundle 外的 0.3.x Python daemon，不要求 bundle current；沒有擷取者維持原拒絕。 | `legacy_l_first_install_crash_loop_recovers_without_current`（三個 legacy 中斷點逐一跑首次 pipx 升級：修正前 `after_capture` 回 `restore_verify_failed`，修正後全綠）；守門 `legacy_l_first_install_recover_still_rejects_unknown_daemon_without_capture`（擷取檔遺失時仍拒絕；修正前即綠，屬守門非紅燈測試） |
| S1-4baca8c-2 | journal 新增 `target{mode,endpoint}`，只在跨模式且目標 endpoint ≠ 舊 endpoint 時寫入（既有 journal 位元組不變）；切換後（entries_switching／start_and_verify／revert_to_previous）中斷時，恢復先以目標模式與 endpoint 停止新版 daemon、`confirm_released` 目標 endpoint，再處理舊 endpoint（對齊 `revert_after_failure`）；目標模式 hold 交給 `restore_supervisor_and_daemon` 撤銷。 | `s1_4baca8c_2_cross_mode_crash_after_start_stops_target_instance_first`（舊 on-demand socket X → `--user` socket Y，`start_and_verify:started` 中斷；修正前 Y 未收到 `daemon.handoff_stop`、恢復失敗；修正後先停 Y，舊版以 X 重新啟動） |
| S1-f3c66b4-2（第 2 份 sol B-2） | 重用 journal `target_autostart` 前要求計畫完整（systemd drop-in 須有 `dropin_path`）；缺欄位時重新取得 hold（drop-in 冪等）；取得後立即把完整紀錄寫回 journal。替身新增 opt-in `FakeServiceCtl::set_strict_dropin`（對齊 `SystemdCtl::release_named`，預設行為不變）。 | `s1_f3c66b4_2_recover_reinterrupted_after_target_hold_completes`（第一次恢復於 `restore_supervisor_and_daemon:after_registration` 再中斷；修正前第二次恢復回 `autostart_release_failed`「hold 紀錄缺少 drop-in 路徑」，修正後完成且 hold 撤銷） |
| S1-f3c66b4-1（第 2 份 sol B-1，High） | 撤回 `2f8f7ed` 的快照排除；`judge_files_preserved` 新增 `expected_overwrites`（預設 None 語意不變）：套件管理 profile 只允許變成新 bundle `versions/0.4.0/py/sw_core/assets/profiles` 的內容（報告列 `package_updated`），遺失或其他內容一律 FAIL。 | `test_managed_profile_overwrite_must_match_bundle_content`、`test_system_driver_keeps_managed_profiles_in_snapshot`（先紅後綠）；Docker 重跑 `od48-55cfa03` systemd-system 的 `default.yaml` 記為 `package_updated` |

最新 sol（`f3c66b4_2179a83`）把上述四條全部標「已修」、累計未修輪數 0。中斷注入一律用 `LEGACY_CRASH_POINTS` 或直接 `set_test_crash_point`，未改動凍結的 `CRASH_POINTS`。

## 裁決的處理（rulings/s1.md 末五列）

| 裁決 | 處理 |
|---|---|
| #1 D3.2 不套 windows-task | 程式不變（`mgmt_com0com` 13 個凍結節點期望不動）；SC-39 compatibility_impact、spec-reconciliation SC-39 列與 rollback.rs 註解記明「只套 systemd 模式」。指示寫「於 SC-38 記明」——見「不成立主張」#1。 |
| #2 D4 模式不一致維持 fail-closed | 程式不變（`DEVICE_STILL_HELD`／`supervisor_mode_mismatch`）；SC-39、spec-reconciliation、release 計畫 §5.1 寫明 hint：以 `setup --user` 或 `setup --system --with-sudo` 指定實際模式，不另取第二個模式的 hold。 |
| #3 Linux cgroup 判定 | sw-pal `ProcScan::proc_cgroup`（POSIX 讀 `/proc/<pid>/cgroup`；Windows／預設 `None`）與純函式 `classify_cgroup`（v2 `0::`、v1 `name=systemd`；葉節點為 `serialwrap.service` 以外的 `*.service`，或 `system.slice` 下的 `*.scope` → `ForeignUnit`；session／終端機 scope → `Session`；根或不明 → `Unknown` 退回父行程判定）。`precheck` 在 MainPID 不相符時先判 cgroup，`ForeignUnit` → 停止前 `DEVICE_STILL_HELD`（`external_supervisor`，附 `cgroup_unit`、hint）；父行程 basename 清單保留為輔助。platform cfg 只在 sw-pal（`check_cfg_boundary` 通過）。先紅回歸：`legacy_d3_4_foreign_service_cgroup_fails_closed_before_stop`（system／user 自訂 unit、容器 scope）、`legacy_d3_4_session_scope_cgroup_is_not_external`；PAL `cgroup_classification_distinguishes_foreign_service_units`、`posix_proc_cgroup_reads_proc_pid_cgroup`、`fake_proc_cgroup_is_scriptable`。Windows 維持父行程判定（LOCAL-WINDOWS-TODO 2）。 |
| SPEC-GAP #5 逐項列出未還原項 | `RestoreProgress`（entries／config／unit／enabled／current）；legacy 停止後還原失敗時，`revert_after_failure` 的 `revert_error` 與 `recover_journal` 的 recovery detail 帶 `unrestored`（依該順序、只列未還原者；還原未開始的早退分支列全部五項）；Rust 對 Rust 路徑輸出不變。先紅回歸：`legacy_spec_gap5_restore_failure_lists_unrestored_items`、`legacy_spec_gap5_recover_restore_failure_lists_unrestored_items`（current 還原失敗 → `["entries","unit","enabled","current"]`）。 |
| SPEC-GAP #10 systemd-system 成功路徑 | 可行並已實跑：driver `--mode systemd-system`（須明確指定，`all` 仍為兩模式、舊檢查 id 語意不變）只驗「0.3.2 system daemon 執行中直接 `setup --system --with-sudo`」成功路徑，必要檢查 13 項（完整集合子集）；Dockerfile 加 sudo 與 swtest 免密碼 sudoers。結果 PASS（見 Docker 節）。 |

## 測試與 gate 數字

| 項目 | 起點（`90da902`） | 終點（`2179a83`） |
|---|---|---|
| `cargo test -p sw-cli -p sw-pal --features test-fakes` | 803 passed／0 failed（上一輪數字） | **814 passed／0 failed**（+11：CLI 8、PAL 3） |
| `cargo test --workspace --all-features` | 943 passed（上一輪） | **954 passed／0 failed／7 ignored** |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 通過 | 通過（每組皆跑） |
| `python3 scripts/rust/check_cfg_boundary.py` | 通過 | 通過 |
| `python3 -m tools.contracts check` | ok | ok（SC 30、IMPLEMENTED 50，集合不變） |
| `python3 -m policy_check --repo .` | 25 pass／0 fail／2 warn | **25 pass／0 fail／2 warn**（R-19、R-22，非 PR 上下文） |
| `regen_lock.py --check`／`--check --base origin/feature/rust-refactor` | OK | OK |
| 觸及的 Python 測試（`test_docker_upgrade`、`test_contracts_catalog`／`_validate`／`_doc_errata`、`test_p1oc_catalog`、`test_check_p4_cases`、`test_rust_p4_case_map`、L1 相關 6 檔） | — | 全綠，只剩下列基準既有失敗 |
| 全套 `python3 -m pytest -q tests/`（序列、前景等待結束） | **8 failed／5290 passed／168 skipped／1 xfailed** | **8 failed／5295 passed／168 skipped／1 xfailed**；失敗集合與起點逐筆相同（新增 0、消失 0） |

起點基準以 `90da902` 的獨立 worktree 在 dev 身分下跑。第一次基準因工作樹在跑的期間被改動而中止作廢；第二次在 worktree 遇 git「dubious ownership」（dev 的 `safe.directory` 未設、worktree 中繼資料屬 root）而多出 39 筆環境性失敗，判為無效，修正擁有權與 `safe.directory` 後重跑得上表數字。中途另在 `f3c66b4` 跑過一次全套：8 failed／5293 passed，失敗集合同樣相同。

基準 8 筆（全部既有、與本分支無關）：`test_doctor::TestWindowsDoctor::test_pyserial_ok_when_importable`、`test_stage0_tx_outcome::test_pyserial_port_write_maps_timeout_to_unknown`、`test_stage0_tx_permit::test_pyserial_chunked_write_drain_stops_after_inflight_chunk`、`test_stage0_tx_permit::test_pyserial_partial_cancel_keeps_cancelled_reason`、`test_stage0_uart_exclusive::TestManagerAttach::test_pyserial_access_denied_raises_device_held_unknown`（容器未裝 pyserial）、`test_rust_bench_tools::test_cargo_workspace_tests_windows_native`（需 Windows 原生）、`test_l1_judge::test_l1_lock_nodeids_subset_of_collect_only`（s0_bc 的 pyserial 參數化 nodeid）、`test_rust_stage1b_registration::test_p4_required_cases_have_passing_evidence`（無 P4 正式證據目錄）。

TDD 紅燈取證：S1-4baca8c-1／-2 與 cgroup 的 CLI 測試，以「新測試＋修正前的 src」實跑確認紅燈後才套用修正；PAL cgroup 三筆先以編譯失敗呈紅；SPEC-GAP #5 兩筆、S1-f3c66b4-2 與 Docker 判定兩筆皆先跑紅再改程式。例外：`test_upgrade_image_grants_swtest_sudo_for_system_mode` 是在 Dockerfile 修改後才寫，未取得紅燈；`legacy_l_first_install_recover_still_rejects_unknown_daemon_without_capture` 為守門測試，修正前即綠。

## 新 L1 節點（`s1a_cargo.linux`、`s1b_cargo.linux` 各 +11、移除 0，`regen_lock --l1-add-only`）

collect 以 `cargo_l1_results.py --list-only`（`--workspace --all-features`／`--workspace`）收集，只保留既有鎖內節點與本輪新節點：

- `cargo:sw-cli::mgmt_legacy_handoff::legacy_d3_4_foreign_service_cgroup_fails_closed_before_stop`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_d3_4_session_scope_cgroup_is_not_external`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_l_first_install_crash_loop_recovers_without_current`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_l_first_install_recover_still_rejects_unknown_daemon_without_capture`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_spec_gap5_recover_restore_failure_lists_unrestored_items`
- `cargo:sw-cli::mgmt_legacy_handoff::legacy_spec_gap5_restore_failure_lists_unrestored_items`
- `cargo:sw-cli::mgmt_legacy_handoff::s1_4baca8c_2_cross_mode_crash_after_start_stops_target_instance_first`
- `cargo:sw-cli::mgmt_legacy_handoff::s1_f3c66b4_2_recover_reinterrupted_after_target_hold_completes`
- `cargo:sw-pal::legacy_supervision::cgroup_classification_distinguishes_foreign_service_units`
- `cargo:sw-pal::legacy_supervision::fake_proc_cgroup_is_scriptable`
- `cargo:sw-pal::legacy_supervision::posix_proc_cgroup_reads_proc_pid_cgroup`

`p4_case_map` 的 `sc39-legacy-daemon-handoff` 為凍結案例（`p4_required.lock.json` 以 sha256 鎖住 tests 清單），本輪未把新節點綁進該案例（見 OWNER-BATCH 1）。

## Docker（三種監管模式，已實跑）

- 映像：`tools/docker/upgrade.Dockerfile` 在 scratch 加「apt 來源改 https＋信任 proxy CA」建置（同上一輪作法），`run-upgrade.sh` 的 scratch 副本只把 `docker build` 換成 `docker tag`、`SCRIPT_DIR` 指回 repo；driver、判定、`--network none`、mount 全部不變；scratch 檔未提交。wheel 以 Python 3.12 由 `git archive v0.3.2` 建置；bundle 為 `mkbundle.sh --release-bundle --profile release --version-override 0.4.0`（開發中 bundle，不取代 release 前置條件 #3）。
- 最終證據 `evidence/stage1/upgrade-docker/od48-55cfa03/`（bundle git revision `55cfa03`）：

| 模式 | verdict | 項數 | `legacy_stop.method` | `waited_ms` | relaunch | 新 daemon 由 setup 啟動 | 舊行程殘留 | two-reader 取樣／重疊 |
|---|---|---:|---|---:|---:|---|---|---|
| systemd-user | PASS | 19 | `systemctl_stop` | 5351 | 0 | 是 | 無 | 1476／0 |
| on-demand | PASS | 19 | `rpc_daemon_stop` | 5214 | 0 | 是 | 無 | 1562／0 |
| systemd-system | PASS | 13 | `systemctl_stop` | 5311 | 0 | 是 | 無 | 1623／0 |

  systemd-system 另驗 system unit active＋enabled、MainPID 為新 daemon、無殘留 hold drop-in、狀態相容、doctor 綠燈；`/etc/serialwrap/profiles/default.yaml` 記為 `package_updated`（覆寫成新 bundle 權威內容）。
- 觀察項：`OBS-UPGRADE-ROLLBACK-PIPX`、`OBS-UPGRADE-STALE-CURRENT`（OD-49 已知限制）、on-demand 的 `OBS-0.3.2-ONDEMAND-START`；systemd-system 新增 `OBS-0.3.2-SYSTEM-UMASK`（0.3.2 system unit `UMask=0117` 使 daemon 自建目錄為 0660，`ensure_runtime_dirs`／ttyMCU／lock 依序 PermissionError、unit 反覆重啟——0.3.2 既有缺陷，與 0.4.0 無關；driver 以一般 umask、不帶 `XDG_RUNTIME_DIR` 前景暖機一次 0.3.2 daemon 後再 setup）與 `OBS-SYSTEM-PROFILES-MANAGED`（system setup 每次以套件 profiles 覆寫 `/etc/serialwrap/profiles` 同名檔，0.3.2 同語意）。
- 中間證據 `od48-2f8f7ed/` 的 systemd-system 判定當時排除了管理檔（S1-f3c66b4-1），已由 `od48-55cfa03` 取代（release 計畫 §4.1 已註明）。
- systemd-system 開發過程的 try1～try5 失敗皆為上述 0.3.2 UMask／暖機 `XDG_RUNTIME_DIR` 與管理檔判定問題，不是 0.4.0 setup 的失敗。

## 不成立主張

1. **裁決 #1「於 SC-38 記明範圍」**：`contracts/behavior_changes.yaml` 沒有 SC-38（OD-21 保留給 `SERIALWRAP_SERIAL_BACKEND`，屬 P5 分支），OD-48 的契約項是 SC-39。範圍改記在 SC-39 與 spec-reconciliation SC-39 列。請編排者確認原意是 SC-39。

## SPEC-GAPS

1. **cgroup 分類的保守誤判**：`system.slice` 下的任何非 serialwrap `*.service`（例如從 cron.service 啟動、或 tmux server 本身跑在使用者 `tmux.service` 底下時，由它啟動的 on-demand 舊 daemon）都會被判為外部監管者而 fail-closed。這是安全方向的誤判，hint 會請使用者先停用該監管者；設計未定義「非監管者的 service」例外清單，本輪不自行加白名單。
2. **跨模式恢復目標為 systemd-system 時**：`recover_journal` 一向以 `with_sudo=false` 執行；目標模式為 systemd-system 時 hold／release 需要 sudo，恢復會以 NEEDS_SUDO 類錯誤停住並保留 journal（既有限制，本輪新增的目標停止沿用同一限制）。
3. **SPEC-GAP #5 粒度**：`config` 在 config.yaml 與 profiles／supervisor 目錄都複製完成後才標已還原；config.yaml 已還原但目錄複製失敗時仍列 `config`。
4. **systemd-system Docker 範圍**：只驗直接 setup 成功路徑；rollback、手動降級、重新升級與 system 模式的 legacy 拒絕路徑未在容器跑。
5. **Docker bundle 的 python**：rulings/s1.md 已裁決 `run-upgrade.sh` 改用 `/usr/bin/python3.12` build venv，指定 OD-48 合入主線後在 release 前置分支處理；本分支未修改該部分（雲端以 3.12 venv 的 `python3` 執行 host 端）。

## OWNER-BATCH

1. **`sc39-legacy-daemon-handoff` P4 案例追加本輪 11 個新節點**：案例已凍結（sha256 鎖住 tests 清單），`check_p4_cases --freeze --add-only` 只能追加新案例；把新節點綁進既有案例需擁有者逐名授權。不追加不影響 L1 鎖的覆蓋。

## LOCAL-WINDOWS-TODO

1. `WindowsProcScan::proc_details` 與 D1.3 的 one-file／pipx launcher 判定的 Windows 原生實測（延續上一輪）。
2. NSSM、services.exe 服務、Task Scheduler 包起來的 0.3.2 daemon 停止前 fail-closed 的實機驗證（Windows 不用 cgroup，維持父行程判定）。
3. Windows 0.3.2 → 0.4.0 在 daemon 執行中直接 setup 的實跑（release 計畫 §4.2）。
4. `s1a_cargo.windows`、`s1b_cargo.windows` 的 add-only 登記以 Windows 原生收集（本輪 11 個新節點中，`posix_proc_cgroup_reads_proc_pid_cgroup` 為 `cfg(unix)`，Windows 不存在）。
5. Windows 原生 `cargo test` 執行本輪新測試。

## BLOCKED-OWNER

- OWNER-BATCH 1（凍結 P4 案例的 tests 清單變更）。
- `sc39-legacy-daemon-handoff` 的 P4 正式證據（`target/p4-evidence/`）仍未產生；`test_p4_required_cases_have_passing_evidence` 與起點同樣失敗（整個 P4 證據目錄缺席，需正式證據管線）。

## 未完成項

- LOCAL-WINDOWS-TODO 1～5。
- release bundle（非 `--version-override`）的 Docker 重跑屬 release 前置條件 #3，不在本輪範圍。
- 既有檔案未全面 rustfmt（repo 起點即非 rustfmt 乾淨，本分支不重排既有檔案）。
