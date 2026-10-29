> 審查者：雲端 Claude Opus xhigh（Codex／pi 停用期間代理 sol 單審位，擁有者 2026-10-29 指定）

# feature/rust-refactor-p5 增量審查 `4f8acdf..86b54d8`

範圍：P5 自身 commit 三個，`a443d8d1`（marker）、`d657a7c3`（merge 主線 `8963d07b`）、`86b54d8d`（修 P5-4f8acdf-1～3）。merge 帶入的主線 commit（OD-48 legacy handoff、0.4.0 release 前置、Docker scope cgroup）依指示只看衝突解法，以及合入後有沒有破壞 P5 的既有行為。

## A 前輪 finding 核對

merge 未觸及 `crates/sw-core/src`、`crates/sw-daemon/src`、`sw_core/`。本輪 P5 的 Rust 產品碼只改了兩處：`sw-pal` 的 `serial/posix.rs`（WARNING 字串）與 `proc_scan/mod.rs`（`py_name()`）。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `25ad390` | 2 | 本輪未動 interactive TX 路徑。s21_core.linux 實跑 1730 列，1729 passed；唯一失敗是 emitted_codes 寫入唯讀 worktree，屬環境因素。 |
| 2 | High | 已修 `25ad390` | 2 | 開埠與持久化仍在 actor 外，未改。 |
| 3 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 4 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 5 | Medium | 已修 `25ad390` | 2 | `daemon_main.rs` 未改。 |
| 6 | High | 已修 `25ad390` | 2 | `wal_fs`、`wal.rs` 未改。 |
| 7 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 8 | Medium | 已修 `25ad390` | 2 | `assemble`／`judge` 仍共用 `build_expect()`；新增的未承擔集合檢查用的也是它載入的 lock。 |
| P5-1393573-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-1393573-2 | Medium | 已修 `d8ac988` | 2 | 未改。 |
| P5-5cc4623-1 | High | 已修 `0a7d0929` | 3 | 未改。 |
| P5-5cc4623-2 | Medium | 依裁決不處理 | 3 | 延後裁決仍適用。 |
| P5-70f6ce7-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-3c4e051-1 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3c4e051-2 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3084388-1 | High | 已修 `2472f78` | 3 | `tests/test_event_latency_measure.py` 通過。 |
| P5-3084388-2 | Medium | 已修 `289f7573` | 4 | HEAD 的工具 sha256 `aa8e8bb1…` 與契約 sha256 `b23f9a1a…` 仍和證據 `event-latency-od45-rust-r6-261029.json` 一致，本輪未改這兩檔。merge 改了 sw-pal／sw-cli，下次 gate 實跑的 daemon sha256 必然不同；gate 會在同一次 run 內重量，judge 依 artifact 綁定判定。 |
| P5-3084388-3 | Medium | 已修 `464adec` | 1 | policy R-21 pass。 |
| P5-2472f78-1 | Medium | 已修 `c4cfb96` | 1 | 未改。 |
| P5-4f8acdf-1 | High | 已修 `86b54d8` | 1 | `judge()` 收集各 section 的 `required_set` 與 `additional_required_sets`。lock 中有 `s21_gate:*` 集合未被承擔時，verdict 判 INCOMPLETE 並列出集合名；lock 由 `build_expect` 載入，正式路徑已接上。以現行 lock 與 expect 重算，列出 cap_{interactive_desktop,supervisor_runtime,sys_admin,windows_native} 四個集合，verdict 為 INCOMPLETE。換回修正前的 judge，`test_uncarried_gate_set_blocks_ready` 紅燈（得 READY）。判定粒度仍有缺口，另列 P5-86b54d8-1。 |
| P5-4f8acdf-2 | Medium | 已修 `86b54d8` | 1 | 四項都已補上：`env.source_tree_dirty`、judge 要求其值為 false、`relevant_paths` 加三個 `Cargo.toml`、補測試。換回修正前的 judge，`test_imported_evidence_requires_clean_source_tree` 三案紅燈。`relevant_paths` 仍缺 sw-proto，另列 P5-86b54d8-2。 |
| P5-4f8acdf-3 | Medium | 已修 `86b54d8` | 1 | 非 root 實跑，stderr 為 `serialwrap: 無法確認 /dev/pts/N 的外部 holder（status=permission），BC-04 僅保證 serialwrap 實例互斥`，與 Python `report.status` 及 `daemon_logging.yaml` 的 `uart_holder_scan_unknown` 模板逐字一致。`accept_unverified_warning_matches_python_template` 已 add-only 登記 `s21_core.linux` 並 passed；`non_root_holder_scan_permission_does_not_block_open` passed。 |
| P5-4f8acdf-4 | Low | 依裁決不處理 | 1 | 依收斂規則，Low 移入 low-backlog。本輪未改。 |
| P5-4f8acdf-5 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-6 | Low | 依裁決不處理 | 1 | 同上。`slice21_gate.sh` 的降權段未改。 |
| P5-4f8acdf-7 | Low | 依裁決不處理 | 1 | 同上。檔尾仍多一個空行。 |

## B 新發現

- **P5-86b54d8-1 [Medium]** `scripts/rust/slice21_gate_judge.py:682-688`（`86b54d8`）
  - 問題：新守門的「承擔」判定只看集合 id，有兩個缺口。
    - **(a) 不分平台。** lock 與 section 判定的粒度都是（集合, 平台）：`_judge_test_section` 只以自身平台呼叫 `required_for`，但 `carried` 只記集合 id。OD-55 與 expect 註解已規劃把 windows 能力列登記到同一 id 的 `s21_gate:cap_<cap>.windows`（LOCAL-WINDOWS-TODO）。屆時只要某個 windows section 承擔 `s21_gate:cap_{windows_native,interactive_desktop,supervisor_runtime}`，這三個集合的 linux 列（共 4 列，例如 `cap_interactive_desktop__exec_or_wait_ctrl_c`）就會被當成已承擔，實際上從未執行，gate 回到 P5-4f8acdf-1 的狀態。
    - **(b) 不分 section 類型。** 報告片段自帶的 `additional_required_sets` 也算承擔，連非測試類 section 也算，而 `_judge_non_test_section` 根本不判集合。這等於接受報告自行宣告承擔，與判定器「不接受報告自行宣告的豁免」的原則不符。
  - 失敗情境：以 `tests/test_slice21_gate_judge.py` 的 `case` fixture 實跑，修正前的基準為 READY。
    - (a)：在 lock 加 `s21_gate:cap_interactive_desktop = {linux: [cargo:sw-daemon::launcher::cap_interactive_desktop__exec_or_wait_ctrl_c], windows: [CORE_NODEIDS[0]]}`，此時得 INCOMPLETE（正確）。再設 `additional_required_sets={"windows_native_l1": ["s21_gate:cap_interactive_desktop"]}`，得 `READY_FOR_OWNER_EVIDENCE`，`verdict_reasons` 為空。預期 INCOMPLETE，並列出 linux 平台未承擔。
    - (b)：在 lock 加 linux-only 的 `s21_gate:cap_sys_admin`，得 INCOMPLETE。再在報告的 `l15_ab` 片段加 `"additional_required_sets": ["s21_gate:cap_sys_admin"]`，得 `READY_FOR_OWNER_EVIDENCE`。預期 INCOMPLETE。
    - 現行 lock 與 expect 尚未觸發；(a) 會在 OD-55 規劃的 windows 登記落地時觸發，因此判 Medium，不判 High。
  - 確切修法：
    - `carried` 改為（集合, 平台）對，只從 `TEST_SECTIONS` 內的 section 收集，平台取 `TEST_SECTIONS[name]`。集合來源為該 section 的 `required_set`、`expect["additional_required_sets"][name]`，以及該 section 片段的 `additional_required_sets`。
    - `uncarried` 改為：lock 中所有 `s21_gate:*` 的（sid, platform）對，凡不在 `carried` 中者。
    - 補上述 (a)、(b) 兩個測試。

- **P5-86b54d8-2 [Medium]** `tests/acceptance/slice21_gate_expect.yaml:19-21`（`relevant_paths`，`86b54d8` 擴充）
  - 問題：WAL append 熱路徑依賴 sw-proto，但 `relevant_paths` 沒有涵蓋它。每筆 append 都會呼叫 `sw_proto::wal_types::crc32_hex`（`crates/sw-core/src/wal.rs:469`）與 `WalRecord::to_line()`（`wal.rs:569`，走 `sw_proto::pyjson`），也會用到 `sw_proto::pycompat`（`b2a_base64`、`iso_utc_now`）。因此 sw-proto 變更後，舊的 WSL2 匯入證據仍會被採用。append p50 正是 OD-61 待重測的指標，sw-proto 也曾隨主線 merge 變更（如 `1393573b`）。
  - 失敗情境：在 scratch clone 實跑，未動審查 worktree。匯入證據取 `wal-rotate-writeback-r6/rust-fix1.json`，改 `env.git_revision=86b54d8d…`、`virtualization=wsl2`、`source_tree_dirty=false`、`verdict=PASS`。
    - 在 `86b54d8` 之上加一個只改 `crates/sw-pal/src/wal_fs/posix.rs` 的 commit：得 EVIDENCE_MISSING（正確）。
    - 改成只改 `crates/sw-proto/src/wal_types.rs` 的 commit：`wal-s1w-verdict` 得 `PASS ['匯入證據（owner-wsl2）：全數達標']`，預期 EVIDENCE_MISSING。
  - 確切修法：
    - `relevant_paths` 加 `crates/sw-proto/src/wal_types.rs`、`crates/sw-proto/src/pyjson.rs`、`crates/sw-proto/src/pycompat.rs`、`crates/sw-proto/Cargo.toml`，或直接加整個 `crates/sw-proto`。
    - `test_relevant_paths_cover_build_profile_manifests` 補這些路徑。
    - 以臨時 git repo 補一案「量測 commit 後只改 sw-proto → EVIDENCE_MISSING」。

- **P5-86b54d8-3 [Low]** `tests/acceptance/l1_required.lock.json`（`86b54d8`）
  - 問題：本輪新增的 cargo 節點已登記 `s21_core.linux`，但新增的 pytest 節點沒有 add-only 登記 `s21_gate:pytest_full`，與 F188 的做法不一致。未登記的節點是 `test_uncarried_gate_set_blocks_ready`、`test_imported_evidence_requires_clean_source_tree[True|None|unknown]`、`test_relevant_paths_cover_build_profile_manifests`、`test_wal_s1w_example_records_source_tree_dirty`。
  - 影響：這些節點失敗時，l1_judge 依「任何列失敗即 FAIL」照樣擋下；但若被 skip，不會被判證據不足。
  - 修法：以 `regen_lock --l1-add-only` 登記。

- **P5-86b54d8-4 [Low]** `crates/sw-daemon/src/health.rs:18`
  - 問題：`status_str()` 與本輪新增的 `HolderScanStatus::py_name()` 重複，Python 狀態字串有兩個來源。
  - 修法：`health.rs` 改用 `py_name()`。

- **P5-86b54d8-5 [Low]** `tests/test_slice21_wal_s1w_import.py:104`
  - 問題：`test_wal_s1w_example_records_source_tree_dirty` 只比對原始碼字串。`wal_s1w.rs:310-317` 的實際判定行為（`--untracked-files=no`、全空白即 false、git 失敗得 "unknown"）沒有測試覆蓋。
  - 修法：補一支 cargo 測試，或在臨時 repo 執行 example 驗證 dirty 與 clean 兩種情況。

**merge `d657a7c3` 衝突解法核對**
- `proc_scan/posix.rs`：主線的 `scan_fd_holders` 改由 P5 的 `tty_holders_fail_closed_with` 取代，兩者判準等價。目標 stat 失敗時 PermissionDenied 記為 Permission、其他記為 Unknown；fd 為 NotFound 時略過；`same_tty` 都是 (dev, ino) 或非 devpts 的 rdev。唯一差異是單一 dir entry 錯誤時 P5 版略過整個 pid，但 status 同為 Unknown，仍是 fail-closed。
- `uart_holders_same_uid` 維持 fail-closed，符合 P7-71fe017-4 的 health／rollback 分離原則。
- P5 側只刪了 import 列，沒有遺失其他內容。
- `behavior_changes.yaml` 以 revision 7 為底，保留 SC-37 與 SC-39；`test_contracts_catalog.py` 通過。
- `l1_required.lock.json` 以三方比對（base `897a3cdc`）驗證：每個（集合, 平台）都等於兩側聯集，沒有任何集合兩側同時變更，也沒有遺失。
- 主線帶入的 `legacy_supervision`（14）、`mgmt_legacy_handoff`（35）、`proc_scan`（29）、`proc_holders_namespace`（20）全數 passed。

**驗證紀錄**（非 root uid 1000、Rust 1.96.1、Python 3.12／pytest 9.2.0；審查 worktree 未修改）
- `cargo_l1_results.py --platform linux -- --workspace --features test-fakes -- --skip cap_`：1778 passed、1 failed、28 skipped。s21_core.linux 1730 列無缺；唯一失敗為 emitted_codes 寫入唯讀 `target/`，屬環境因素。
- `--caps posix_pty -- -p sw-pal --features test-fakes`：
  - 與 Python 子集並行的首跑：370 passed、1 failed，失敗的是 `cap_posix_pty__open_failure_after_tiocexcl_clears_excl`。
  - 同範圍無並行重跑兩次：皆 371 passed、0 failed。`s21_gate:linux_cap_posix_pty` 336 列與 `s21_gate:cap_posix_pty` 19 列全數 passed。
  - 根因見既有碼待辦第 1 項，與本輪變更無關。
- Python 子集，涵蓋 slice21_* 11 檔、l1_judge、release_workflow、docker_upgrade、contracts_catalog、resource_markers、event_latency_measure、cargo_l1_results、p4／stage1 登記、l15 `test_case_list_matches_freeze`：727 passed、4 failed、2 skipped。4 支失敗皆為已知：
  - `test_l1_judge.py` 3 支：s0_bc 的 pytest 版本相依 id，以及 s21_core.windows 能力列待 Windows 原生收集（OD-55 LOCAL-WINDOWS-TODO）。
  - `test_p4_required_cases_have_passing_evidence`：雲端無本機 Stage 1 出口證據，依 OWNER-BATCH #5 屬證據缺席。
- 紅燈先行查證：
  - 換回 `a443d8d1` 版 judge：`test_uncarried_gate_set_blocks_ready` 與 `test_imported_evidence_requires_clean_source_tree` 三案皆紅。
  - Rust 新測試：修正前以 `{:?}` 格式化，輸出 `Permission`，必紅。
- 其他檢查：
  - policy check：25 pass、0 fail、2 warn（R-19、R-22）。
  - `check_cfg_boundary.py`：rc 0。
  - `git diff --check 4f8acdf 86b54d8`：無輸出。
  - changelog.d 273 個 fragment 均有 frontmatter。
  - `a443d8d1` 只把函式層 marker 改為與模組一致的 `process_rpc`；模組層 `core_portable` 仍在。

## 既有碼待辦（不計入結論）

1. `crates/sw-pal/src/conformance/mod.rs:170-183`：`posix_pty_pair()` 以 `libc::openpty` 建立 master／slave，沒有設 `FD_CLOEXEC`。同一測試執行檔內，`cap_posix_pty__external_plain_open_before_daemon_is_held` 經 `spawn_plain_holder`（`:238`）fork 子行程時，會把並行測試的 pty fd 一併繼承，被測端的鎖後 holder 掃描因此看到外來 holder。
   - 實測：以 `serial_posix` 能力測試在並行負載下連跑 60 次，失敗 1 次，panic 為 `open: Held(HolderReport { pids: [1247], holder_unknown: false, status: Permission })`；首跑那支失敗也是同一機制。
   - 產品開埠使用 `O_CLOEXEC`（`serial/posix.rs:455`），不受影響。但 `s21_gate:cap_posix_pty` 是 gate 必要集合，會間歇紅燈。
   - 建議：`openpty` 後以 `fcntl(F_SETFD, FD_CLOEXEC)` 設定兩端，或讓 spawn 子行程的案例以 `--test-threads=1` 執行。
2. `scripts/rust/cargo_l1_results.py:421-427`：libtest 把失敗段（panic 訊息）印在 stdout，但這裡只解析 stdout、不轉印，gate log 因此看不到失敗原因（本輪首跑那支失敗的 log 只有 2 行）。建議失敗時轉印 stdout 的 `failures:` 段，或存檔。
3. `crates/sw-cli/tests/emitted_codes.rs:394-401` 仍寫入 repo 的 `target/`（前輪已列）。前輪列的 `test_queue_full_backpressure` 本輪未重驗。

## 對裁決的異議（不計入結論）

無。

結論：需修正
