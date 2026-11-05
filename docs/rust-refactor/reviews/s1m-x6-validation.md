# S1M-x6d：接線與整體驗收

分支 `feature/s1m-x6`。本輪起點 `d929cac4`。產品修正已在前幾輪落地，本輪只補黑箱路徑斷言、凍結鎖 add-only 與本紀錄。未改 Python daemon、契約或 golden。

## 根因

`42b3bc13` 的 `run_supervision_switch` 以 passwd 家目錄（`current_user_home()`）優先於環境變數 `HOME`，systemd user unit 寫到真實家目錄。同一時期 unit 路徑在 `XDG_CONFIG_HOME` 有值時走 XDG，缺省才退回 passwd。Python 基線的 user unit 固定為 `HOME/.config/systemd/user/serialwrap.service`，一般 config／state 仍走 XDG 與 `SERIALWRAP_*` 覆寫。

修正提交：`068c8e54`（監管切換採用 `HOME`）、`6f67a0cb`（寫入、快照、還原與 hold 固定 `HOME/.config`）、`e56c5475`（真實 SystemdCtl hold 根目錄）。黑箱 session 防線在 `78cbc7a3`。

## 入口與命令

沙箱為 `target/s1mx6d-sbox`：`bwrap --ro-bind / /` 只綁定本 worktree 可寫，不把 `~/.config` 蓋成 tmpfs，因此真實 unit 可讀、寫入會得到 `Read-only file system`。PATH 排除 pipx，並設定 `CARGO_NET_OFFLINE`。未呼叫 production daemon，未開真實序列埠。

修前紅燈沿用同一 nodeid 的既有紀錄（`~/b-log/rust-refactor/exec/s1m/x5/round-2.log` 第 45234–45312 行，副本 `target/s1mx6d-red-reused.log`）。失敗在 `assert result.returncode == 0`，早於本輪新增的路徑斷言：

`寫入 user unit 失敗：… <home>/.config/systemd/user/serialwrap.service … Read-only file system`

候選 bundle 由目前產品碼重新建置，未沿用舊執行檔：

```bash
bash target/s1mx6d-sbox scripts/rust/mkbundle.sh --dev --out "$PWD/target/s1m-x6-bundle"
```

CLI sha256 `1aaa9697a9469ba623bef9a35ce8d22f160a52fb8a4cc8c7bcdd8038d6d0befc`，manifest `git_revision` 為 `d929cac4`。

綠燈：

```bash
bash target/s1mx6d-sbox env \
  SERIALWRAP_AB_LABEL=rust-cli-python-daemon \
  SERIALWRAP_CLI_EXE="$PWD/target/s1m-x6-bundle/bin/serialwrap" \
  SW_BUNDLE="$PWD/target/s1m-x6-bundle" \
  SW_RESOURCE_REPORT="$PWD/target/s1m-x6-blackbox.json" \
  python3 -m pytest -q -rs --confcutdir=tests/contract_blackbox \
  tests/contract_blackbox/test_cli_mgmt_supervision_switch.py::test_supervision_set_rust_only_switch
```

## 驗收結果

| 檢查 | 結果 |
|---|---|
| 指定 Rust 黑箱 | `1 passed`、0 skipped；L1 `exit_code=0`、`finished=true` |
| `cargo test --workspace` | 1034 passed、0 failed、7 ignored；exit 0 |
| `cargo test --workspace --features test-fakes` | 1034 passed、0 failed、7 ignored；exit 0 |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit 0 |
| cfg 邊界、依賴方向、`tools.contracts check` | exit 0；契約 `ok: true`、`findings: []` |
| `tests/test_resource_markers.py` | 9 passed |
| `regen_lock.py --check --base 42b3bc13` | exit 0 |

真實 user unit 在沙箱測試前後的存在性、mtime_ns、mode 與 sha256 相同（`350f9a251dd94b3092c1af287fad3d68ba351bf06b2f4919dee1419584517b97`）。本輪未跑全套 pytest、Docker 或 Stage 出口 driver。

凍結鎖只經 `--l1-add-only`，linux、selector 不變、移除 0。新增均為 `s1mx6` 回歸：`s1a_cargo` 與 `s1b_cargo` 各 5 筆；`s1a_blackbox` 與 `s1b_20groups` 各 7 筆 harness 防線（後者的既有 selector 會收集到這些案例）。selector 還收集到其他子項尚未鎖定的 nodeid，本輪未登記。


## 第 2 輪：R-21 減敏與基準比對

本輪起點 `9886208c`，只修改本文件。審查 `S1-9886208-1` 成立：以既有 R-21 gate 先確認 `24 pass / 1 fail / 2 warn`，命中原第 17 行；將個人家目錄減敏為 `<home>` 後為 `25 pass / 0 fail / 2 warn`，exit 0。未新增測試、修改掃描器或增加豁免，凍結鎖維持原狀。

本輪證據位於 `target/s1mx6d-r2/`；修前、修後 Policy 分別為 `policy-red.log`、`policy-green.log`。前輪 Rust 修前紅燈 `target/s1mx6d-red-reused.log` 已核對同一 nodeid 與 `Read-only file system` 訊息。

新 bundle 以 `scripts/rust/mkbundle.sh --dev --out "$PWD/target/s1m-x6-r2-bundle"` 重新建置，保留前輪 bundle。來源 HEAD 為 `9886208c`；本輪差異僅驗收文件，產品碼相同。CLI sha256 為 `1aaa9697a9469ba623bef9a35ce8d22f160a52fb8a4cc8c7bcdd8038d6d0befc`；manifest sha256 為 `babc77c289948429bc8458e9409194cd6e0cfdaa577885034ba955d7a0d95146`，詳見 `identity.json`、`build.log`。

沙箱入口為 `bash target/s1mx6d-r2/sbox-valid`，保留真實家目錄唯讀、私有 `/dev`、`/proc`、`/tmp`、`/run`，並隔離 XDG 目錄、移除 production pipx PATH 與 Docker endpoint 環境變數。Windows 掛載被遮蔽，僅補回唯讀 DNS resolver。所有命令逐項前景執行；完整 argv、exit code 與摘要保存在 `gates.json`、`final-suites.json`。

| 檢查 | 本輪結果 |
|---|---|
| 指定 Rust 黑箱 | `1 passed`、0 skipped；`blackbox-l1.json`：`exit_code=0`、`finished=true` |
| R-21 既有防線與 resource markers | `16 passed`（其中 markers 9 案），exit 0 |
| `cargo test --workspace` | `1034 passed / 0 failed / 7 ignored`，exit 0 |
| `cargo test --workspace --features test-fakes` | `1034 passed / 0 failed / 7 ignored`，exit 0 |
| Windows GNU workspace check | exit 0 |
| cfg、依賴方向、契約 | 全部 exit 0；契約 `ok=true`、`findings=[]` |
| `regen_lock.py --check` 及 `--check --base 42b3bc13` | 全部 exit 0；鎖檔未改 |
| 排除明禁項目的 `pytest -q tests/`，前景一次 | `5702 passed / 11 failed / 152 skipped / 3 deselected / 1 xfailed`；另有 `201 subtests passed`，exit 1 |
| `9886208c` 原始內容重跑上述失敗案例 | 同一組 11 個 nodeid 全部重現失敗，exit 1 |
| Policy（減敏後） | `25 pass / 0 fail / 2 warn`（R-19、R-22），exit 0 |

pytest 明確排除整個 `tests/contract_blackbox/test_cli_mgmt_update.py`，並 deselect `test_docker_l15.py::test_docker_smoke_two_container`、`test_docker_upgrade.py::test_docker_upgrade_0_3_2_to_0_4_0`、`test_docker_upgrade.py::test_docker_install_sh_upgrade_from_0_3_2`。未執行 Docker 或出口證據 driver 實跑；未排除的全套 pytest 門檻未驗證，不記為通過。Python 輪的 `rust_only` 仍依裁決 SKIP。

基準重跑先將唯一差異文件還原為 `git show 9886208c:<path>` 的內容，確認 `git diff --name-only 9886208c` 為空，再只跑 11 個失敗 nodeid；結束後恢復減敏候選。`pytest-tests.log`／`.xml` 與 `baseline.log`／`.xml` 分別保留原始失敗；`baseline-comparison.json` 確認集合相同。失敗 id 如下：

```text
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t1_wal_reset_preserves_console
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t5_bind_idempotent_preserves_console
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t6_human_sees_agent_commands
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t7_agent_runs_while_human_types
tests/test_human_agent_coexist.py::TestHumanAgentCoexist::test_t8_full_run_simulation
tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python
tests/test_l15_smoke.py::test_release_inflight_runs_on_python
tests/test_p1oc_socket_perm.py::test_socket_group_chgrp_to_supplementary_group
tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native
tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
tests/test_stage0_uart_exclusive.py::TestManagerAttach::test_preopened_port_reused_by_probe_and_bridge
```

可觀察的失敗分組：human-console 5 案皆為 `vtty ... 在讀者握住前已消失`；L1.5 smoke 2 案嘗試寫入唯讀 `<home>/b-log/func-test-reports`，維持原始 `INSUFFICIENT_EVIDENCE`；socket group 案為實得 GID 1000、預期 65534；Windows 原生 Cargo L1 與 P4 註冊案缺少證據；UART mock 案為 `_attach_threads` 的 `KeyError`。上述皆在起點重現，本輪未診斷或修復範圍外的產品／harness 問題。

初次沙箱環境另造成兩個 Cargo 失敗：`rpc_client_conformance::resolve_tcp_gai_matches_python`、`rpc_client_conformance::tcp_refused_and_gai_and_timeout0_match_python`。先停用網路、後遮蔽 `/mnt`，使 WSL 的 `/etc/resolv.conf -> /mnt/wsl/resolv.conf` 不可用，得到 `EAI_AGAIN(-3)`。補回唯讀 resolver 後取得正確的 `EAI_NONAME(-2)`；原組 7 案全綠，兩種 workspace 全部通過。原始失敗 log 未覆蓋，未修改 golden 或斷言。

真實 user unit 的存在性、mtime_ns、mode 與 sha256 在所有測試及基準重跑前後相同；見 `unit-before.json`、`unit-after-suites.json`。Python daemon、契約、golden 與凍結鎖均未修改。

SPEC-GAPS：未排除的全套 pytest 與 Docker 實跑受 brief 禁令限制，交由編排者協調；Windows／P4 證據不足維持原判定，其中擁有者／硬體項目維持 `BLOCKED-OWNER`。既有碼待辦：`crates/sw-cli/tests/support/mod.rs` 未使用 import 警告（Low，不修）；上述 11 個起點失敗不在 x6d 修正範圍。
