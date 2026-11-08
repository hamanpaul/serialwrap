# S1M-x6b 第 2 輪：真實 hold 控制器的範圍缺口

起點為 `6f67a0cb54b9ec7b7d9130a117c6d6fba2832e49`，分支為 `feature/s1m-x6`。本輪只新增此報告，未修改產品、測試、契約、凍結鎖或失敗判準。

## 審查項目處理

| 項目 | 嚴重度 | 處理 |
|---|---|---|
| A-1 DUT TX checkpoint | Medium | already fixed `978aa1e1`；沿用本輪提供的非實作者審查結論，不重做其他子項。 |
| A-2 Ctrl 事件孫行程同步 | Medium | already fixed `51bd8fa5`；同上。 |
| A-3 出口 driver 保留原始失敗 | Medium | already fixed `ddcbdde0`；同上。 |
| A-4 升級環境 PyYAML 隔離 | High | already fixed `12b3a929`；同上。 |
| A-5 發佈 bundle 不含 test-fakes | Medium | already fixed `12b3a929`；同上。 |
| B：S1-6f67a0c-1 | Medium | **未修，SPEC-GAPS-1**：必要修改位於本 brief 未列入的 `crates/sw-pal/src/service_ctl/systemd.rs`。依本輪「需要範圍外的檔案時停下該項」規則停止修復，不把靜態確認或 FakeServiceCtl 測試計為 fixed。 |

A 段沒有標為「未修」的項目；A-6 為 Low，不另處理。ITEM 1 的 HOME 優先 resolver 保留 `068c8e54` 的實作。

## SPEC-GAPS

### SPEC-GAPS-1：修正實際 drop-in 寫入需要擴充檔案範圍

本 brief 的產品修改位置是 `crates/sw-cli/src/setup_cmd.rs` 與 `crates/sw-cli/src/rollback.rs`，回歸位置是 `crates/sw-cli/tests/mgmt_supervision_switch.rs` 與 `crates/sw-cli/tests/mgmt_update.rs`。審查指出的實際寫檔控制器在 `crates/sw-pal`，不在這份修改位置清單。沒有從裁決取得擴充本子項檔案範圍的明示授權，因此本輪不改該檔，也不以 CLI 層補丁迴避控制器的路徑分歧。

目前程式直接支持以下路徑差異：

- `crates/sw-cli/src/setup_cmd.rs::systemd_user_unit_path` 固定從已解析 HOME 組 `.config/systemd/user/serialwrap.service`；`crates/sw-cli/src/rollback.rs:4257` 透過 `home_dir(ctx)` 使用它。
- `crates/sw-pal/src/service_ctl/systemd.rs:55` 的 `hold_path` 在 systemd-user 分支先取非空 `XDG_CONFIG_HOME`；`hold_named` 在同檔第 248 行使用這個路徑實際寫檔。兩個 HOME 相同、XDG 不同時，計畫與實際 drop-in 仍會分歧。
- `crates/sw-cli/src/rollback.rs:1920` 的 `stop_supervisor:after_hold` 在第 1922～1924 行將實際 hold 回寫 journal 之前執行。此時磁碟 journal 仍可能指向 HOME 計畫路徑。
- `crates/sw-cli/tests/mgmt_supervision_switch.rs:465` 的既有回歸透過 `with_supervisor_state` 啟用 FakeServiceCtl（`tests/support/mod.rs:1224`）；假的 `hold_autostart` 在非 strict 模式回 `dropin_path: None`（`crates/sw-pal/src/fakes/service.rs:411`），由 `merge_hold_plan`（`crates/sw-cli/src/rollback.rs:4407`）補回計畫，沒有測到真實 SystemdCtl 的 XDG 分支。
- Python 基線 `sw_core/setup_cmd.py:543` 以 `home / _USER_UNIT_REL` 寫 user unit，`_USER_UNIT_REL` 在第 296 行固定為 `.config/systemd/user/serialwrap.service`；一般 config 仍在第 34～52 行保留 SERIALWRAP／XDG 優先序。本輪沒有以 Python 基線提出不成立主張。

因此，第 1 輪報告中「修後 hold 案例驗證實際 hold」僅適用於 FakeServiceCtl 回傳紀錄，不能推論真實控制器的實際檔案路徑與中斷恢復已修好。本 finding 保留未修；未執行真實 SystemdCtl 的 crash／recovery 先紅回歸，不宣稱已證實完整失敗後果。

後續需要的授權是：將 `crates/sw-pal/src/service_ctl/systemd.rs` 的 systemd-user hold 路徑納入本子項。既有 `TestEnv::with_real_service`（`crates/sw-cli/tests/support/mod.rs:1255`）可作為真實控制器的回歸入口；不能混用優先選擇 FakeServiceCtl 的 `with_supervisor_state`。回歸仍集中在原指定檔案。獲授權後，須先以真實 SystemdCtl、隔離 HOME／XDG、`stop_supervisor:after_hold` 中斷，確認紅燈，再修路徑並驗證實際 drop-in 清除與 Restart／RefuseManualStart 還原；XDG 有值與缺省都要覆蓋。不得只靠 FakeServiceCtl 通過。

其他子項、S1 A/B 的 `graceful_stop.rs`、Docker、slice gate、出口證據 driver 均不處理；擁有者及硬體項目維持 BLOCKED-OWNER。

## 不成立主張

無。範圍缺口不代表 finding 不成立，也不是延後或已知限制裁決。

## 既有碼待辦

`crates/sw-cli/tests/support/mod.rs::write_history_backup` 仍從 config_dir 組歷史 user unit 路徑；延續第 1 輪待辦，本輪未修改。

## 驗證邊界與起點證明

所有測試都在前景透過既有 `bash target/sbox` 執行並等候結束。bwrap 將 `/` 掛為唯讀，僅本 worktree 可寫；獨立 PID／網路／IPC namespace，隔離 `/dev`、`/proc`、`/tmp`、`/run`，空目錄遮蔽真實 `.config`、`.local/state`、`.local/bin`，PATH 排除 pipx 入口。沒有執行 production daemon／RPC、真實序列埠、Docker 或其他 worktree 操作。

本輪所有產品碼及測試碼仍為起點 `6f67a0cb`，完整 pytest 及 workspace 均直接在該起點執行。因此本輪失敗本身就是起點實測，無須另外建立快照重跑，也不以第 1 輪數字代替。本機來源證明為 `target/s1mx6b-r2-baseline-proof.json`。

一般 config／state 的既有 16 組覆寫矩陣包含在 `mgmt_supervision_switch` 的 `s1mx6_` 選取內；其餘路徑盤點與 Python／契約相容性分析沿用同目錄 `s1mx6b-user-unit-paths.md`，產品碼未變。這些相容性檢查不取代未完成的真實控制器回歸。

## 測試與 gates

| 指令（均以 `bash target/sbox` 執行） | 本輪結果 |
|---|---|
| `cargo test -p sw-cli --features test-fakes --test mgmt_supervision_switch s1mx6_` | 3 passed、0 failed；含既有 16 組 config／state 矩陣。hold 仍為 FakeServiceCtl 證據。 |
| `cargo test -p sw-cli --features test-fakes --test mgmt_update s1mx6_` | 1 passed、0 failed。 |
| `cargo test -p sw-cli --features test-fakes` | 690 passed、0 failed、3 ignored；exit 0。 |
| `python3 -m pytest -q tests/`，前景一次 | 5696 passed、10 failed、163 skipped、1 xfailed、14 warnings、201 subtests passed；exit 1；1543.54 秒。 |
| `cargo test --workspace` | 824 passed、2 failed、6 ignored；exit 101；在失敗 target 停止，後續部分 target 未執行，不宣稱 workspace 全綠。 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0。 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0。 |
| `python3 -m tools.contracts check` | `ok: true`、`findings: []`，exit 0。 |
| `python3 tests/acceptance/regen_lock.py --check` | 案例鎖／屬性快照／統計檔及 L1 鎖結構 OK，exit 0；未修改鎖。 |
| `python3 -m policy_check --repo .` | 25 pass、0 fail、2 warn（R-19、R-22）、0 exempt；exit 0。 |

本機原始日誌統一為 `target/s1mx6b-r2-*.log`，沙箱指令保留於 `target/sbox`。本輪沒有修改 Python 原始碼或 Python 測試。

### pytest 失敗 ID（起點直接實測）

| 失敗 ID | 當次失敗證據 |
|---|---|
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console` | vtty 在讀者握住前已消失；stderr 有 tmux `error creating /tmp/tmux-1000/default (No such file or directory)`。 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t5_bind_idempotent_preserves_console` | 同上。 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t6_human_sees_agent_commands` | 同上。 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t7_agent_runs_while_human_types` | 同上。 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t8_full_run_simulation` | 同上。 |
| `tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python` | runner 嘗試寫真實 HOME 的 `b-log/func-test-reports`，被唯讀掛載拒絕；保留 INSUFFICIENT_EVIDENCE。 |
| `tests/test_l15_smoke.py::test_release_inflight_runs_on_python` | 同上。 |
| `tests/test_p1oc_socket_perm.py::test_socket_group_chgrp_to_supplementary_group` | `assert 1000 == 65534` 失敗。 |
| `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` | 缺少 `target/p4-evidence/l1/windows/cargo/resource-report.json`；保留證據不足。 |
| `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence` | 缺少 P4／L1 出口證據，judge 回報 28 項 FAIL；另列六個 BLOCKED-OWNER，未執行出口證據 driver。 |

10 個 ID 與第 1 輪報告一致，但本輪的起點證明來自以上重新完整執行，不是沿用前輪快照或把失敗視為可忽略。沒有修改 tmux／runner／群組權限／出口證據判準。

### workspace 失敗 ID（起點直接實測）

- `sw-pal::rpc_client_conformance::resolve_tcp_gai_matches_python`
- `sw-pal::rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python`

兩案在網路隔離 namespace 中，實際得到 `[Errno -3] Temporary failure in name resolution`／`Some(-3)`，golden 預期 `[Errno -2] Name or service not known`／`Some(-2)`。兩案同樣直接在未修改的 `6f67a0cb` 起點碼執行；未改 golden、斷言或網路沙箱，也未改判 PASS。

REMAINING: S1-6f67a0c-1 未修；需將真實 SystemdCtl 的必要檔案納入範圍後，完成先紅回歸、修正及中斷恢復驗證。
