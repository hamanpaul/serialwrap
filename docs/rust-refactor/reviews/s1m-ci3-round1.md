# S1M-ci3 第 1 輪修正報告

起點：`5cac8744bb28b780b18ffa952cd1d9b199a42f6f`；分支：`feature/s1m-ci3`。

## High／Medium 與上一輪審查

本輪為 ci3 第 1 輪，brief 未提供適用於 ci3 的上一輪 A／B finding；依指定 job log 處理 34 個失敗節點。其他分支的 review 不列入修正。

## 逐支失敗處理

以下 34 列對應指定 hosted Windows job log。實作提交為 `7c4db73a`；兩支資源報告的本機 Windows 完整驗證仍受 SG-CI3-01 阻擋。

| 失敗節點 | 根因與修法 | 狀態 |
|---|---|---|
| `tests/test_build_fingerprint.py::TestSetupRestartsStaleDaemon::test_mode_transition_does_not_add_a_restart` | 替身只模擬 RPC 停止，卻洩漏至 Windows 真實 PID 探測（無 PID 回 unknown）；行程存活探測改接同一份替身狀態。 | fixed `7c4db73a` |
| `tests/test_device_handoff.py::TestProbeExternalHolder::test_probe_detects_holder_in_fake_proc` | 同上；以假的 uid 與真實暫存檔 stat 描述 holder，無需 Windows symlink 權限。 | fixed `7c4db73a` |
| `tests/test_functest_harness.py::test_main_continues_after_runner_exception_and_writes_report` | 預設 pty 在 Windows 先被能力守衛攔下，未進入假的 runner；改以 bridge／scenario 路徑呼叫既有 runner 替身，不開 COM。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_alias_path_same_inode_matched` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_missing_endpoint_st_rdev_tolerated` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_missing_fd_st_rdev_tolerated` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_posix_char_device_different_rdev_not_matched` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_posix_char_device_rdev_matching` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_same_path_matched` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_same_rdev_different_devpts_instance_not_matched` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_holder_probe_portability.py::TestHolderProbePortability::test_zero_rdev_not_used_as_identity_evidence` | 假 POSIX procfs 未提供宿主缺少的 getuid／major；補齊 API 替身與 fd stat fixture，保留 inode／rdev 斷言。 | fixed `7c4db73a` |
| `tests/test_realhw_pin_gate.py::test_realhw_report_deployed_content` | fixture 使用規格禁止的絕對 sidecar dir；改用安裝根的正斜線相對路徑。 | fixed `7c4db73a` |
| `tests/test_regression_windows_caps.py::test_regression_report_deployed_content` | 同上，修正 sidecar fixture；量測器與判定不變。 | fixed `7c4db73a` |
| `tests/test_resource_report.py::test_resource_report_deployed_content` | 同上，修正 sidecar fixture；保留 start／end digest、daemon_env_missing 與 daemon_outside_install 斷言。 | fixed `7c4db73a`（版面實作）；Windows 完整驗證待 SG-CI3-01 |
| `tests/test_resource_report.py::test_resource_report_contract_blackbox_measures_deployed_content` | 同上，修正 sidecar fixture；保留 start／end digest、daemon_env_missing 與 daemon_outside_install 斷言。 | fixed `7c4db73a` |
| `tests/test_resource_report.py::test_resource_report_cli_without_daemon_env_unmeasured` | 同上，修正 sidecar fixture；保留 start／end digest、daemon_env_missing 與 daemon_outside_install 斷言。 | fixed `7c4db73a`（版面實作）；Windows 完整驗證待 SG-CI3-01 |
| `tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native` | 不修改測試本身；僅 hosted Windows CI deselect，出口 driver 照常強制。 | CI deselect `7c4db73a`；不修測試本體，理由見本列 |
| `tests/test_rust_bench_tools.py::test_ab_selection_follows_frozen_selector_per_platform` | 不修改測試本身；僅 hosted Windows CI deselect，出口 driver 照常強制。 | CI deselect `7c4db73a`；不修測試本體，理由見本列 |
| `tests/test_rust_dep_direction.py::test_allowed_layout_passes` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[cli_to_core]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[cli_to_daemon]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[cli_to_core_dev_only]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[pal_to_proto]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[proto_to_pal]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[core_to_cli]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[daemon_to_cli]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_violation_is_reported[core_to_libc]` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_dep_direction.py::test_missing_member_is_tool_error` | CI 只有 rustup proxy，未選定工具鏈；job 明確安裝／選定 1.96.1 與雙 target，SW_REQUIRE_CARGO=1。libc fixture 路徑另以 as_posix 保持合法 TOML。 | fixed `7c4db73a` |
| `tests/test_rust_goldens_fresh.py::test_golden_is_fresh[gen_pycompat.py-pypath.posix.tsv]` | 假 pwd 表卻仍 import 宿主 pwd，Windows 無此模組；以固定模組替身提供相同表。 | fixed `7c4db73a` |
| `tests/test_rust_goldens_fresh.py::test_golden_is_fresh[gen_pyre.py-pyre.tsv]` | Windows checkout 把 pyre 語料改成 CRLF，而產生器明確拒絕；以 .gitattributes 固定 LF，保留拒絕檢查。 | fixed `7c4db73a` |
| `tests/test_rust_goldens_fresh.py::test_golden_is_fresh[gen_runtime_paths.py-runtime_paths.posix.tsv]` | POSIX probe 實際使用 Windows os.path／平台分支；和 Windows probe 一樣明確指定欲量測的平台 seam。 | fixed `7c4db73a` |
| `tests/test_rust_goldens_fresh.py::test_golden_is_fresh[gen_runtime_paths.py-runtime_paths.windows.tsv]` | 乾淨 probe env 移除了 Windows 載入 _overlapped／Winsock 必需的 SystemRoot；僅攜入此 OS 必要變數。 | fixed `7c4db73a` |
| `tests/test_rust_goldens_fresh.py::test_pyre_limits_is_fresh` | Windows checkout 把 pyre 語料改成 CRLF，而產生器明確拒絕；以 .gitattributes 固定 LF，保留拒絕檢查。 | fixed `7c4db73a` |
| `tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence` | 不修改測試本身；僅 hosted Windows CI deselect，出口 driver 照常強制。 | CI deselect `7c4db73a`；不修測試本體，理由見本列 |

## CI 與凍結鎖

- 三個 CI deselect 節點均不在任何 Windows 凍結必要集合。`tests/acceptance/l1_required.lock.json` 不變。
- `scripts/rust/stage1_exit_driver.sh` 的 `pytest_full_junit` 保持完整 `tests/` 且不帶 deselect。
- hosted Windows 報告不再宣稱是原凍結 `s0_win_pytest` selector 的正式出口證據；judge 與正式 Windows runner 不變。
- 缺 Cargo 的既有 fixture 語意不變；此 job 明設 `SW_REQUIRE_CARGO=1`，不得以缺工具 skip 放行。

## 額外必要的 fixture 收尾

`test_resource_report.py` 會在子程序跑 `tests/test_wal.py`。本機 Windows／UNC 首次在原始 WAL 測試得到 **9 failed, 2 passed, 1 skipped**：常駐 writer 尚未關閉，暫存目錄就被清理，回 `WinError 32`。

`7c4db73a` 在原有 9 個 fixture 加 `contextlib.closing`，讓所有既有斷言完成後先關 writer 再清理目錄。重跑子集合為 **1 failed, 10 passed, 1 skipped**；剩餘的輪替前提失敗見 SG-CI3-01。未改任何平台標記、節點名或斷言；9 個受影響測試檔共 694 個既有斷言的 AST 與起點一致（`assertion-audit.log`）。

## 驗證

所有執行輸出位於本 worktree 的 `target/s1m-ci3/`。測試以前景執行並等待結束；本輪未跑完整 pytest、Docker、slice gate 或出口 driver。

Linux 測試使用 bwrap：真實 `/` 唯讀，只有本 worktree 與映射至 `/tmp` 的本輪暫存區可寫；隔離 `/dev`、`/proc`、`/run` 與 PID。涉及 Rust CLI 管理命令的 Rust 測試也在此沙箱內。後續沙箱另以唯讀空目錄遮蔽真實使用者的安裝入口、設定與狀態。Rust workspace 測試保留網路，以維持既有 DNS 錯誤判定；未操作 production daemon 或實體 UART。

Windows 使用 CPython **3.12.3**，Rust metadata 使用下載至本 worktree、依官方 channel manifest SHA-256 核對的 **1.96.1** 原生 Windows 工具；沒有全域安裝。`PYTHONUTF8=1`，測試 HOME／USERPROFILE／TEMP 均在本 worktree。WSL 啟動曾回 `UtilAcceptVsock`，改由既有 C: CPython 啟動本輪隔離 runtime；不修 WSL。原生 Git 的合成 repo 僅在本程序以環境設定信任本 worktree 子目錄，未改全域 Git 設定。

| 驗證 | 實際結果 | 輸出 |
|---|---|---|
| Linux 受影響 13 個測試檔（含 resource／CI／marker），排除需真實出口證據的兩支整合檢查 | **256 passed, 2 deselected** | `linux-affected.log` |
| Linux WAL fixture／resource report／marker | **50 passed** | `linux-wal-fixtures-final.log` |
| Linux 最終 CI workflow（含版本 tag pin） | **10 passed** | `linux-ci-final.log` |
| Windows 擴大集合（resource、dep direction、CI、build fingerprint、holder、handoff 等） | **113 passed, 6 failed**；其後兩支 Git fixture 已過，四支資源報告仍見 SG-CI3-01 | `windows-affected-final.log` |
| Windows Git 合成 repo，補足本程序 trust context 後重跑 | **2 passed** | `windows-git-fixtures-final.log` |
| Windows WAL 原有 9 支＋四支資源報告＋最終 CI workflow | **18 passed, 5 failed**；5 個失敗 ID 見 SG-CI3-01 | `windows-wal-fixtures-final.log` |
| Windows golden 新鮮度 | 原生 3.12.3 的 **13 支全過**；所在一輪總計 **31 passed, 2 failed**，兩個失敗為資源報告，原始輸出保留 | `windows-green1.log` |
| `cargo test --workspace` | **1041 passed, 0 failed, 7 ignored** | `cargo-workspace.log` |
| `cargo test --workspace --features test-fakes` | **1041 passed, 0 failed, 7 ignored** | `cargo-test-fakes.log` |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | exit **0** | `cargo-windows-check.log` |
| `python3 scripts/rust/check_cfg_boundary.py` | exit **0** | `cfg-boundary.log` |
| `python3 -m tools.contracts check` | `ok: true`、**0 findings** | `contracts-final.log` |
| `python3 tests/acceptance/regen_lock.py --check --base 5cac8744` | 案例鎖、屬性、統計與 L1 基準比對均過；凍結鎖與起點逐位元組相同 | `regen-lock-final.log` |
| Policy（正式 PR #248 既有 release／doc-reference 脈絡，對 main 檢查本分支） | **24 pass, 0 fail, 1 warn, 2 exempt** | `policy-context-final.log` |

Cargo 的 7 個 ignored 保留原設定，未計入 passed；其中既有擁有者項目維持 **BLOCKED-OWNER**。本輪沒有 COM 或實體 UART 證據。

回歸轉紅證據：新增 CI 結構／語料行尾回歸在修前 **3 failed**（`workflow-red.log`）；Windows `core_to_libc` 以舊反斜線 fixture 在真正原生 Cargo 上 **1 failed, 10 deselected**（`windows-cargo-red.log`），修後完整依賴方向檔的 11 支納入上述 113 passed；WAL 的修前／修後數字如上。沒有新增硬體回歸 case：此變更由 pytest 與測試工具覆蓋。

Policy 裸命令的 R-07 失敗來自起點已有的 `VERSION=0.4.0`、最近 tag `v0.3.2`。已以 `gh pr view 248 --repo hamanpaul/serialwrap --json headRefName,baseRefName,labels,url` 核對正式 [PR #248](https://github.com/hamanpaul/serialwrap/pull/248) 的既有 `release:0.4.0` 與 `policy-exempt:doc-reference`（`pr248-context.json`）；context run 沿用這兩個已存在的 label，R-09 changelog 檢查通過且未使用 `skip-changelog`。起點 VERSION 未改；沒有建立 tag、修改 PR label 或 push。最終裸命令為 **24 pass, 1 fail, 2 warn**（`policy-bare-final.log`，唯一 FAIL 為 R-07）；正式脈絡另一次 exit 0，沒有覆寫原始失敗。

## 不成立主張

無。

## SPEC-GAPS

### SG-CI3-01 — BLOCKED-OWNER：Windows／UNC 的 WAL 輪替與完整資源報告驗證

原生 Windows 在本 worktree 的 UNC 路徑仍得到 `rotated=[]`，以下現有輪替前提斷言失敗：

- `tests/test_wal.py::TestWalTailLatest::test_tail_raw_latest_scope_is_current_file_after_rotation`

因此下列四支資源報告外層的子程序 exit 不為 0，仍維持 FAIL：

- `tests/test_resource_report.py::test_resource_report_written`
- `tests/test_resource_report.py::test_resource_report_deployed_content`
- `tests/test_resource_report.py::test_resource_report_cli_without_daemon_env_unmeasured`
- `tests/test_resource_report.py::test_resource_report_plugin_works_under_confcutdir`

原始輪替案例在未加 closing 的本機重現中已失敗，當時清理例外會遮住輪替前提；其所有斷言未改。`sw_core/wal.py`、`sw_core/wal_fs.py`、`sw_core/win32_files.py` 與起點逐位元組一致。hosted Windows 原始 log 沒有這個 WAL 失敗；UNC／NTFS 差異是待驗推論，內部根因**不確定**。

本輪依 OD-75 與 brief 停止此項，未改 Python daemon、判定器或 frozen lock，未把 FAIL 改成 PASS／SKIP。需要擁有者核准 NTFS 驗證位置，或編排者另派 Python WAL 調查，才能完成這五個 nodeid 的原生 Windows 綠燈。

兩支有關部署內容的測試已修正 sidecar；Windows 產出的 start 量測已可辨識安裝樹，`daemon_env_missing` 也正確；其中完整黑箱量測案例已通過。上述四支外層仍失敗的結果保留，不以部分欄位正確宣稱整支通過。

## 既有碼待辦

- SG-CI3-01 的 Python WAL／UNC 輪替問題留待裁決，不在此分支改 daemon。
- `tests/test_config_profiles.py:229` 的既有 invalid escape SyntaxWarning，未修改。
- Policy 的既有 R-19 安裝行解析警告保留，不擴張修正。
- 對 main 的 R-22 嚴格檢查另報 3 個既有引用缺口（`policy-context-without-doc-exemption.log`）：`plans/03-stage0-test-infra.md` 的舊 changelog fragment 與 `runner._wait_for_socket`，以及 `plans/06-slice21-daemon-core.md` 的舊 changelog fragment。這些不在 ci3 變更中；正式 PR 已有 `policy-exempt:doc-reference`，本輪未修改其文件或新增豁免。

## 交付狀態

- 實作：`7c4db73a fix(ci): 修正 Windows pytest 可攜性與工具鏈`。
- Python daemon、Rust 產品 crates、`contracts/rpc_schema.json`、`tools/uart_stdio_bridge/src` 與凍結鎖均未修改。
- 未 push、未建立 PR、未 merge、未部署；由 driver 後續 fast-forward push／送審。

## REMAINING:

- **BLOCKED-OWNER：SG-CI3-01** 的 1 支 WAL 輪替＋4 支資源報告原生 Windows 驗證。
