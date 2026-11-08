# S1M-x6b：user unit 路徑盤點與回歸

本輪起點為 `068c8e54`（ITEM 1 已完成），分支為 `feature/s1m-x6`。只統一 systemd-user unit 的寫入、快照、還原及 hold 計畫；一般 config／state 仍依既有環境覆寫規則解析。

## 實作及相容性

- `setup_cmd::systemd_user_unit_path(home: &Path)` 接受已解析的 HOME，組成 `.config/systemd/user/serialwrap.service`；`start_new` 與 rollback 共用。
- rollback 的 `systemd_user_unit_path(ctx)` 先呼叫 `setup_cmd::home_dir(ctx)`，不再以 XDG、passwd home 或 config_dir 另組 user unit 路徑。
- 已核對四個消費點：`install_supervisor_held` 寫新 unit、`query_supervisor` 擷取註冊、`restore_registration` 還原註冊，以及 `planned_switch_hold` 預存 drop-in 計畫。
- Python 基線 `sw_core/setup_cmd.py::_user_dirs`（原第 34 行）對一般設定採 `SERIALWRAP_CONFIG_DIR > XDG_CONFIG_HOME > HOME/.config`；`_start_new`（原第 543 行）則以 `home / _USER_UNIT_REL` 寫 user unit，兩者本來就不同。
- `contracts/cli_schema.json` 的 `SERIALWRAP_STATE_DIR`／`XDG_STATE_HOME`（3018／3024）、`SERIALWRAP_CONFIG_DIR`／`SERIALWRAP_CONFIG`／`XDG_CONFIG_HOME`（3036／3042／3048）維持原契約。
- `lib.rs::env_snapshot`（88）保留 `SysEnv::vars()` 完整快照與 Windows 大小寫語意；未發現本項的 env 遺失證據。
- `endpoint_resolve.rs::config_write_path`（358）仍採非空 `SERIALWRAP_CONFIG`，其次 `ctx.paths.config_yaml`，寫入不走系統唯讀 fallback。
- `setup_cmd.rs::state_path`（原 1464）在一般模式採 `ctx.paths.state_dir/state.json`；systemd-system 仍查指定 run_user 的 `user_home()`。`/etc` 路徑及該 run_user 語意均未修改。
- `tests/contract_blackbox/bb_l15.py::run_nodeid_in_pidns`（245）以 `dict(os.environ)` 複製環境，只移除 `SW_RESOURCE_REPORT`、補回 A/B 選擇器並加入外層 PID namespace 記號；HOME／XDG 保留。本項不修改 namespace 或 spawn 機制。

## 全 crate 搜尋盤點

搜尋範圍為整個 `crates/sw-cli`，查詢 `current_user_home`、`getpwuid`、HOME／USERPROFILE／HOMEDRIVE／HOMEPATH、XDG 與 systemd unit 字面路徑。下表為逐處分類；行號以本輪實作前後附近位置定位，函式名為穩定入口。crate 內沒有直接 `getpwuid` 或新增 libc 查詢。

| 位置 | 類別與判定 |
|---|---|
| `src/lib.rs:68` `PlatformHome` | `HomeLookup` 轉接器，交給 `RuntimePaths::resolve`／`pypath` 判斷 env 優先序；不自行組 unit 路徑。 |
| `src/bench_state.rs:33` `SysEnvHome` | 轉接器；bench state 與 benches.yaml 使用 runtime_paths resolver。 |
| `src/install_layout.rs:112` `PlatformHome` | 轉接器；`expanded_path`／`expanded_path_without_normalizing` 把環境快照傳入 `pypath::expanduser`。 |
| `src/endpoint_resolve.rs:45` `EnvHome` | 轉接器；明示 config 路徑展開使用環境快照，寫入另見上節。 |
| `src/doctor_cmd.rs:227` `EnvHome` | 轉接器，供 `pypath` 展開；沒有 user unit 路徑組裝。 |
| `src/commands/profile_cmd.rs:323` `SysEnvHome` | 轉接器，`py_expanduser` 接收 env。 |
| `src/commands/daemon_cmd.rs:1343` `EnvHome` | 轉接器，沒有改變 HOME 優先序。 |
| `src/setup_cmd.rs:741` `home_dir` | env 優先 resolver：POSIX HOME；Windows USERPROFILE，其次 HOMEDRIVE＋HOMEPATH；再退回 PAL current_user_home。已由 ITEM 1 承接。 |
| `src/commands/daemon_cmd.rs:1270` `process_home` | 讀取行程環境的 home resolver，用於 daemon env 展開；不組 user unit 路徑。 |
| `src/commands/daemon_cmd.rs:1283` `context_home` | context env 優先 resolver，HOME／Windows env 之後才退回 PAL。 |
| `src/setup_cmd.rs:42` `user_dirs` | 實際組一般 config／data 路徑；保留 SERIALWRAP > XDG > HOME 優先序。 |
| `src/setup_cmd.rs` `systemd_user_unit_path` | 唯一 production user unit 字面路徑組裝，固定已解析 HOME。 |
| `src/rollback.rs` `systemd_user_unit_path` | 轉接上述 helper；四個呼叫點全部沿用同一路徑。 |
| `src/rollback.rs` `planned_switch_hold` | 從上述 unit 的 parent 組 `serialwrap.service.d/90-serialwrap-update-hold.conf`。systemd-system 分支仍為 `/etc/systemd/system`。 |
| `src/setup_cmd.rs:790`／`start_new` 的 systemd-system 分支 | HOME 下的 system unit staging；特權安裝到 `/etc/systemd/system/serialwrap.service`，未修改。 |
| `src/rollback.rs` 的 install／query／restore systemd-system 分支 | 實際 `/etc/systemd/system/serialwrap.service` 路徑與 sudo 命令，未修改。 |
| `tests/support/mod.rs:282,328` | 隔離 HOME／USERPROFILE 環境值；state/config 另設 SERIALWRAP 覆寫。 |
| `tests/support/mod.rs:1922`、`tests/mgmt_layout.rs:40`、`tests/mgmt_connect.rs:344` | 測試用 HomeLookup 轉接器。 |
| `tests/endpoint_resolve.rs:56,148`、`tests/mgmt_com0com.rs:107`、`tests/event_cmd.rs:29`、`tests/skill_cmd.rs:24` | 固定假家目錄／None 的替身；不呼叫真實 passwd。 |
| `tests/support/mod.rs:1241`、`tests/mgmt_setup.rs:1490,1802` | HOME user unit 夾具與路徑斷言，符合 Python 基線。 |
| `tests/support/mod.rs:1080` `write_history_backup` | 歷史備份夾具另外讀 `config_dir/systemd/user`，與實際 unit 不一致；列入既有碼待辦，本項回歸使用新建 update 快照，不經此歷史夾具。 |
| `tests/mgmt_supervision_switch.rs` 的 `s1mx6_supervision_home_precedes_passwd`、crash-after-hold 及本輪案例 | 隔離 HOME 的路徑斷言；新增假 passwd／XDG 矩陣不讀取真實家目錄內容。 |
| `tests/mgmt_update.rs` 本輪案例 | HOME／XDG／passwd 三個不同內容的 unit 夾具，驗快照與失敗還原。 |
| `tests/mgmt_daemon.rs:189`、`tests/mgmt_layout.rs:107`、`tests/delegate_resolve.rs:34`、`tests/mgmt_terminal.rs:101` | env 注入與測試輸入，非 production 路徑 resolver。 |
| `tests/emitted_codes.rs:204-212,372` | HOME／XDG 等名稱的診斷碼排除白名單與其斷言，不操作路徑。 |

## 先紅回歸

在產品碼仍為 `068c8e54` 時，兩個新增案例各以 XDG 有值、缺省兩種順序執行，四次均 exit 101，且失敗都是路徑或快照內容斷言：

- `s1mx6_unit_hold_ignores_xdg`：XDG 有值時 plan 指向 `xdg-config/systemd/user`，缺省時指向 `passwd-home/.config/systemd/user`，而非 HOME。
- `s1mx6_unit_snapshot_restore_ignores_xdg`：備份 `registered.unit` 分別取得 XDG／passwd sentinel，而非 HOME sentinel。
- 本輪另增 `s1mx6_config_state_override_matrix`，交叉 HOME 有值／缺省、XDG 有值／缺省、SERIALWRAP 目錄覆寫有值／缺省與明示 SERIALWRAP_CONFIG，共 16 組，檢查 RuntimePaths、setup 一般 config 及 config 寫入路徑。相容性測試預期在修前即通過，不聲稱為 bug 的紅燈。

修後 hold 案例驗證實際 hold 與 plan 的 drop-in 路徑、Restart 值，下一次 setup 復原後 hold 狀態、紀錄與三個候選 drop-in 路徑均無殘留。update 案例以 health 失敗觸發還原，驗證 HOME 快照及還原內容、XDG／passwd 同名檔未變與 hold 釋放。全部沿用原 crash／backup 夾具，無需實體序列埠回歸案例。

## 執行邊界

所有測試前景執行於 bwrap：`/` 唯讀，只將本 worktree 掛為可寫；獨立 PID／網路 namespace、`/dev`、`/proc`、tmpfs `/tmp` 及 `/run`。真實 `.config`、`.local/state` 由沙箱空目錄遮蔽；完整 suite 另遮蔽 `.local/bin`，所有執行均從 PATH 排除 pipx 入口。沒有存取真實 UART、production daemon、其他 worktree 或 Docker。

執行指令與原始日誌保存在本 worktree 的 `target/sbox`、`target/s1mx6b-*.log`；target 為本機證據，未追蹤。全套 pytest 依使用者指令後段步驟 (3) 執行一次；不執行 slice gate、出口證據 driver 或 Docker。

## 審查處理與範圍外項目

- S1M-x6b 路徑偏差（原 S1M-x6 High 的本子項）：已實作上述修正。
- ITEM 1（env HOME 優先）：already fixed `068c8e54`。
- 本輪未提供上一輪審查 A／B 逐項清單，不能宣稱已核銷未收到的 findings。
- 不成立主張：無。
- SPEC-GAPS：其他子項的黑箱 harness 防線、Docker L1.5 與出口 driver 由其 owner 承接；不在本項宣稱通過。S1 A/B `graceful_stop.rs` 等其他分支裁決不在本項修改範圍。擁有者／硬體項目維持 BLOCKED-OWNER。
- 既有碼待辦：上述 `tests/support/mod.rs::write_history_backup` 的歷史 systemd-user 路徑與 HOME 不一致，未修改。

## 驗證結果

以下命令均經 `bash target/sbox` 執行並等待結束；沒有修改任何失敗判準。

| 命令／範圍 | 實際結果 |
|---|---|
| `cargo test -p sw-cli --features test-fakes --test mgmt_supervision_switch s1mx6_` | 3 passed、0 failed（含 ITEM 1 既有案例及本輪 16 組相容性矩陣） |
| `cargo test -p sw-cli --features test-fakes --test mgmt_update s1mx6_` | 1 passed、0 failed |
| `cargo test -p sw-cli --features test-fakes` | 690 passed、0 failed、3 ignored |
| `python3 -m pytest -q tests/`，完整前景執行一次 | 5696 passed、10 failed、163 skipped、1 xfailed、14 warnings、201 subtests passed；exit 1；1567.36 秒 |
| `068c8e54` 快照中逐 ID 重跑上述 pytest 失敗 | 相同 10 個 ID 全部 failed；exit 1；19.13 秒 |
| `cargo test --workspace` | 824 passed、2 failed、6 ignored；exit 101，失敗 target 之後的部分 target 未執行 |
| `cargo test --workspace --features test-fakes --no-fail-fast` | 1028 passed、2 failed、7 ignored；exit 101，所有 target 均執行完；僅下列同一組 DNS 失敗 |
| `068c8e54` 快照的 `cargo test -p sw-pal --test rpc_client_conformance` | 5 passed、相同 2 failed；exit 101 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0 |
| `python3 -m tools.contracts check` | `ok: true`、`findings: []`；exit 0 |
| `python3 tests/acceptance/regen_lock.py --check` | 案例鎖／屬性快照／統計檔及 L1 鎖結構皆 OK；exit 0；未修改任何鎖 |
| `python3 -m policy_check --repo .` | 25 pass、0 fail、2 warn（R-19、R-22）、0 exempt；exit 0 |

沒有觸及 Python 原始碼或測試；上表完整 pytest 包含既有 Python 相容性檢查。不得把完整 pytest 或 workspace 說成全綠。

### pytest 失敗 ID 與起點對照

本輪與起點快照的失敗 ID 完全一致；快照由 `git archive 068c8e54` 建立在本 worktree 的 `target/s1mx6b-baseline`，未建立或修改其他 worktree。兩個產品碼檔案與下列案例所在的五個測試檔均逐位元組核對 `git show 068c8e54:<path>`。

| 失敗 ID（本輪及起點皆 FAIL） | 相同失敗證據 |
|---|---|
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console` | tmux `error creating /tmp/tmux-1000/default (No such file or directory)`，vtty 在讀者接手前消失 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t5_bind_idempotent_preserves_console` | 同上 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t6_human_sees_agent_commands` | 同上 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t7_agent_runs_while_human_types` | 同上 |
| `tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t8_full_run_simulation` | 同上 |
| `tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python` | runner 嘗試在 `HOME/b-log/func-test-reports` 寫報告，被真實 HOME 唯讀沙箱拒絕；保留 INSUFFICIENT_EVIDENCE |
| `tests/test_l15_smoke.py::test_release_inflight_runs_on_python` | 同上 |
| `tests/test_p1oc_socket_perm.py::test_socket_group_chgrp_to_supplementary_group` | sandbox 的群組設定下 `assert 1000 == 65534` 失敗 |
| `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` | 缺少 `target/p4-evidence/l1/windows/cargo/resource-report.json`；保留證據不足 |
| `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence` | 缺少 P4／L1 出口證據（judge 回報 28 項 FAIL）；另外六個擁有者機台項目維持 BLOCKED-OWNER，未執行證據 driver |

本項不修改 tmux、func-test runner、群組權限測試或出口證據驗證器；也不為取得綠燈而開放真實 HOME 寫入。

### workspace 失敗 ID 與起點對照

- `sw-pal::rpc_client_conformance::resolve_tcp_gai_matches_python`
- `sw-pal::rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python`

本輪與 `068c8e54` 快照都在同一無外網 namespace 中得到 `[Errno -3] Temporary failure in name resolution`，golden 預期 `[Errno -2] Name or service not known`；斷言與 golden 均未改動。此為沙箱下可重現的起點失敗，不在 S1M-x6b 修改範圍。`--no-fail-fast` 僅讓失敗後的 target 繼續執行，仍回 exit 101。

完整原始證據為 `target/s1mx6b-pytest.log`、`target/s1mx6b-pytest-baseline.log`、`target/s1mx6b-baseline-proof.json`、`target/s1mx6b-workspace.log`、`target/s1mx6b-workspace-fakes.log`、`target/s1mx6b-rpc-baseline.log` 及其他同前綴 gate 日誌。

REMAINING: none（限本 brief 的 S1M-x6b；上述起點失敗與範圍外出口／擁有者證據維持原狀態）。
