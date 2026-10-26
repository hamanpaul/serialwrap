# P6-T8 與 P7 前段（T0／T1／T11／T18A）非實作者審查（Sonnet）

- 審查日期：2026-10-26
- 審查分支：`review/p6t8-p7front`（基於 `origin/feature/rust-refactor-p7` @ `5af6c15`）
- 審查範圍：`4b9d399`、`ccd90bd`（P6-T8）；`984ea3f`、`1d013f8`、`02faf78`、`99c7452`、`5af6c15`（P7 T0／T1／紅燈／T11／T18A）
- 依據：`docs/rust-refactor/serialwrap-rust-spec-final.md`、`owner-decisions-2026-10-15.md`（OD-14..OD-20）、計畫 07 Task 8／08 Task 0,1,11,18A、`origin/docs/parity-amendments` 的 `parity-amendments-2026-10-26.md` 與其修訂後 07／08 計畫
- 立場：非實作者；未修改任何程式或測試，只新增本報告。

## 驗證方法與結果摘要

| 項目 | 方法 | 結果 |
|---|---|---|
| P6-T8 plan 測試 | `cargo test -p sw-pal --features test-fakes --test console_conformance -- --include-ignored --skip windows_com` | 22/22 PASS；預設跑 15 passed／10 ignored（7 `cap:posix_pty`＋3 `cap:windows_com`） |
| 平台 cfg 邊界 | `python3 scripts/rust/check_cfg_boundary.py`；對 sw-core／sw-proto／sw-cli 的 diff grep `cfg(`／`libc::`／`target_os` | exit 0；只有 `#[cfg(test)]`，平台謂詞僅在 sw-pal |
| Windows 編譯 | `cargo check --workspace --tests --target x86_64-pc-windows-gnu` | 通過（Windows 本體為 `todo!` 骨架） |
| Golden 新鮮度 | `capture_p7_goldens.py --check`（Python 3.11、3.12）；另以 3.12 `--out` 重擷取後 `diff -r` | 兩者皆 exit 0、十九檔逐位元組相同；`pytest tests/test_p7_goldens_fresh.py` 3 passed（3.12） |
| Golden 於 3.13 | `python3.13 … --check` | `login_fsm_scenarios.ndjson` STALE（計畫只要求 3.11／3.12，列為資訊，見 L5） |
| T11 差分（自建） | 在 scratchpad 建獨立 crate 呼叫 `validate_rule_dict`，與 Python `validate_rule_dict` 比對：324 個手寫邊界輸入＋5842 個隨機 regex（含 alternation／群組／lookaround／條件式／possessive／backref／旗標） | 邊界 324 筆僅 4 筆不同（見 L1、L2）；隨機 regex 5842 筆 **0 差異**（含 suspicious 判定） |
| T11 測試 | `cargo test -p sw-core --test event_schema`、`-p sw-proto --test redos`、`-p sw-core --test p7_skeleton` | 15／2／3 passed |
| T18A | `pytest tests/contract_blackbox/test_bb_faketarget_p7_selftest.py`（3.12）；P3 `test_bb_harness_selftest`、`test_bb_no_product_import`、`test_bb_daemon_e2e` | 9 passed；P3 三檔通過 |
| T18A 預設位元組 | 自寫差分：同一串互動（錯誤帳號、錯誤密碼、echo、Ctrl-C、CR/LF 混用）分別對 base `e5e2c56` 與 HEAD 的 `bb_faketarget.py` 執行 | 輸出與 `rx_log` 逐位元組相同 |
| 鎖／凍結 add-only | `git show 984ea3f` 對 `cases.lock.json`／`cases.yaml` 的刪除行；`regen_lock.py --check`；`tools.contracts check` | 刪除行僅 `cases_revision`；`--check` OK；contracts `ok:true` |
| 其他 pytest | `test_contracts_catalog`／`test_acceptance_cases`／`test_contracts_slice22`／`test_slice21_case_freeze`／`test_p7_skeleton_boundary`／`test_resource_markers`（3.12） | 通過（3.11 因 3.12 語法 f-string 於 `test_resource_markers` 解析失敗，base 同，屬環境差異） |
| 既有紅燈 | `test_bb_faketarget_s21.py`（P5 的 FakeTarget 擴充） | base 7 failed → HEAD 6 failed（`emit` 一案轉綠）；非本批引入 |

## 發現

### M1（Medium）`held_slave_paths` 仍以 readlink 字串比對，與修訂後 07 Task 8（#40／#64／E-109）矛盾

- 位置：`crates/sw-pal/src/proc_scan/posix.rs:51-72`（`held_fd_targets`）、`crates/sw-pal/src/console_pty.rs:100`（`held_slave_paths` 接線）；測試缺口見 `crates/sw-pal/tests/console_conformance.rs`。
- 事實：實作逐字重現 Python `_enumerate_all_held_paths`（`sw_core/uart_io.py:1394-1419`）——把 `/proc/<pid>/fd/*` 的 readlink 字串放進 `HashSet<String>`，`has_external_peer` 以 `slave_path in held` 判定。本實作對「原 plan 07 Task 8 Step 3.2」是忠實的。
- 矛盾：`parity-amendments-2026-10-26.md` #40／#64（B:E-109、F-S06）把此點判為 ALIGN-TO-PYTHON，要求 console reaper／`held_slave_paths()` 改用 06 Task 2 #64 的 PAL `same_tty`（`(st_dev, st_ino)` 身分；devpts 不以 rdev 判定），使「另一 mount namespace 同名 `/dev/pts/N`」不被誤計為 peer，並移植 `TestUartBridgeHeldPathsNamespace` 5 案為 `console_reaper_ignores_other_namespace_same_name_pts` 等（`uart_io.py:1430-1471`）。目前 HEAD 沒有 `same_tty`（`grep -rn same_tty crates` 只有兩個無關測試名），也沒有這 5 案。
- 重現（概念）：`unshare --mount --fork` 另掛一個 devpts newinstance，在其中開啟 `/dev/pts/<N>`（N 與 console slave 同號）；`held_slave_paths()` 會含 `/dev/pts/<N>` 字串，`has_external_peer` 回 true，孤兒 console 永不被 reaper 回收。
- 修法：`ConsoleTransportFactory::held_slave_paths` 改回傳以 `(st_dev, st_ino)` 為鍵的集合（或新增 `held_tty_ids()`），`has_external_peer` 以 `stat(slave_path)` 取得的身分比對；沿用 P5 `proc_holders` 的 `same_tty`（p5 分支 commit `0600442`）；補 5 個 namespace 案例與 `tcp_accept_sets_nodelay_and_label_shape`（#109）。
- 註：這批 commit 早於修訂文件，非實作者過失；但在 T9 消費前必須補，否則 ConsoleHub reaper 以此為準。

### M2（Medium）`RecoverPlan::AwaitDrain` 骨架與修訂後計畫（#57）相反

- 位置：`crates/sw-core/src/recover.rs:17-18`。
- 事實：骨架註解寫「drain 進行中：加入 06 StopOp waiters，回條到達後再重新 plan」。修訂後 08 Task 9 把此變體改為 `DrainBusy`（立即回 `SESSION_RECOVERING`＋`drain_stalled`／`drain_timeout_epoch`，不掛 waiter、不 reattach），原 `AwaitDrain` 作廢；修訂文件 §3 也把 `recover.rs:18` 列為需改的已實作項。
- 修法：重新命名為 `DrainBusy` 並更新註解；`crates/sw-core/tests/p7_skeleton.rs` 若有引用同步改；P7 紅燈測試改依 `recover_during_drain_returns_session_recovering_immediately`。

### M3（Medium）P7-T0 未建立修訂後要求的 `p7_gate:py_parity` 與 Python 特性測試入鎖

- 位置：`984ea3f`（`tests/acceptance/l1_required.lock.json` 未動；commit 訊息自述「l1_required.lock 不登記」）。
- 事實：修訂後 08 Task 0 新增 Step，要求 `p7_gate:py_parity`（linux／windows）add-only 登記 `tests/test_console_unlock.py` 17 筆、`tests/test_event_dispatcher.py::{test_per_rule_concurrency_drops_oldest,test_timeout_kills_handler}`、`tests/test_event_engine.py::test_disable_clears_counter`（#111／#112）。目前 `l1_required.lock.json` 的 sets 只有 `s0_*`／`s1a_*`／`s1b_*`。
- 修法：以 `tests/acceptance/regen_lock.py --l1-add-only` 建立 `p7_gate:py_parity` 集合並登記上列 nodeid；Task 19 gate 引用。同理 T11 紅燈後須登記新增的 Rust nodeid（`event_schema`／`redos`）到對應 P7 集合。

### L1（Low）超長數字字串 `int()` 的錯誤訊息與 Python 不同

- 位置：`crates/sw-core/src/event_engine/schema.rs:148`（`py_int`，委派 `sw_proto::pycompat::py_int_parse`）。
- 重現：`cooldown_ms: "9"*5000` → Python（3.11/3.12）`ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 5000 digits; use sys.set_int_max_str_digits() to increase the limit`；Rust `ValueError: invalid literal for int() with base 10: '9999…'`。錯誤類別一致，訊息不同；此訊息會經 registry 的 `str(exc)` 進入 `INVALID_RULE_SCHEMA` 回應。
- 修法：在 `py_int` 的 `Str` 分支，去除空白、正負號與底線後數字位數 > 4300 時回 `Value("Exceeds the limit (4300 digits) for integer string conversion: value has {n} digits; use sys.set_int_max_str_digits() to increase the limit")`；補一列 golden（`capture_p7_goldens.py` 的 event_schema 案例）。

### L2（Low）`(?P>x)` 的 regex 錯誤訊息少一個字元

- 位置：`crates/sw-proto/src/pyre/parse.rs:509`（P3 程式，由 T11 schema 訊息暴露）。
- 重現：`pattern: {"kind":"regex","value":"(?P>x)"}` → Python `pattern.value is not a valid regex: unknown extension ?P> at position 1`；Rust `… unknown extension ?P at position 1`。
- 修法：Python 在 `?P` 後再取一字元並接在訊息後（`"unknown extension ?P" + char`）；`char` 為 EOF 時改報 `unexpected end of pattern`。補 `redos`／`event_schema` golden 一列。

### L3（Low）Windows 原生 `caps.com0com_console` 已為 `true`，但本體仍是 `todo!`

- 位置：`crates/sw-pal/src/lib.rs:290`；`crates/sw-pal/src/console_com0com/windows.rs`、`console_tcp/windows.rs`、`console_mux/windows.rs`（8／8／15 行骨架）。
- 事實：Windows `native()` 宣告 com0com 可用，但 `open_com0com_console`／`bind_tcp_console`／`new_mux` 在 Windows 上會 `todo!("P6 Task 8")` panic。骨架階段可接受，且清單已寫在 commit 訊息「LOCAL-WINDOWS-TODO」與 changelog；但 repo 內只有 `testutil.rs` 四處註解，沒有集中清單。
- 修法：把 LOCAL-WINDOWS-TODO（`console_tcp/windows.rs bind`、`WinConsoleMux`、`console_com0com/windows.rs open`、testutil 的 WinSocket nodelay／reuseaddr、`comm_api_calls`、`toggle_peer_modem_lines`）集中寫進 07 Task 8 的「Windows 待辦」段或 `docs/rust-refactor/` 下的清單；在實作完成前 Windows `native()` 的 `com0com_console` 維持 `false`，或讓這些入口回 `Unsupported` 而非 panic。

### L4（Low）`cap_posix_pty__pty_slave_is_raw_and_master_nonblocking` 的 D-01 斷言為空轉

- 位置：`crates/sw-pal/tests/console_conformance.rs:62-80`（`no_symlink_points_to(&pair.slave_path, &[run_dir])`）。
- 事實：`run_dir` 是測試剛建立的空暫存目錄，內部不可能有 symlink，斷言恆真；與計畫範例相同，並非偏離，但無法偵測 PAL 在 run dir 建 stable symlink 的回歸。
- 修法：改檢 `native()` 實際使用的 console/run 目錄（由 `Platform` 提供或 `SERIALWRAP_RUN_DIR` 指向的隔離目錄），建立 PTY 前後各掃一次，斷言無新增指向 `slave_path` 的 symlink。

### L5（Low／資訊）流程與穩定性備註

1. **T18A 未獨立成 PR**：計畫要求「規格 §10.4 測試工具變更須獨立審閱，本 Task 以獨立 PR 合併」。`5af6c15` 與 T11 同在 `feature/rust-refactor-p7`；請在合併時另開 PR 或在 PR 描述註明已取得獨立審閱。
2. **T18A 時序案例的 CI 穩定性**：`test_d_boot_script_timing_within_100ms`（±100 ms）與 `test_e_noise_interval_within_20_percent`（12–19 行）在負載高的 runner 可能 flaky；本機單次通過。依 CLAUDE.md「已知 flaky 不得放行」，建議保留 ±100 ms 但加一次「連跑兩次集合相同」的穩定性證據。
3. **T18A (a) 案例**：計畫寫「以 P3 `test_bb_harness_selftest.py` 同一組互動驗證逐位元組不變」，實作改為自寫互動序列。已以自建差分補驗（base 對 HEAD 逐位元組相同），不構成缺陷。
4. **Golden 在 Python 3.13 不穩**：`login_fsm_scenarios.ndjson` 於 3.13 STALE；計畫只釘 3.11／3.12，但請在 `capture_p7_goldens.py` 對 3.13+ 明確拒絕（如同 re2 偵測的 exit 2），避免日後誤重擷取。
5. **SC-30 與修訂文件 PA-OD-1**：`984ea3f` 登記的 SC-30（run 立即取消）正是修訂文件 PA-OD-1 列為「會改變外部可觀察輸出、待擁有者決定是否沿用」的 Rust-only 差異之一。目前登記為 `APPROVED`／`absolute_only`，待 PA-OD-1 裁決後可能需回調；不阻擋本批。
6. **T0 前置檢查退出碼**：`check_p7_prereqs.py` 目前 exit 1（缺 `pyre::escape`）。commit 已將它標為 BLOCKED-UPSTREAM（只擋 Task 3），處置合理；Task 3 開工前須由 P3 補 `pyre::escape`。

## 計畫測試與契約核對（無發現項）

- **P6-T8 計畫表 20 案**：全部存在且斷言與計畫一致或更嚴（`select_console_kind_table` 多了 `AUTO`／`Tcp\n`；`winsock_error_message_format` 於 POSIX 另驗 `[Errno `）；紅燈 commit `4b9d399` 到實作 commit `ccd90bd` 之間測試檔未被削弱（`git diff 4b9d399 ccd90bd -- crates/sw-pal/tests` 無差異）。
- **SC-28**：`select_console_kind` 對 `(None,true)`、`("tcp",true)`、`(" PTY ",true)`、`("pty",false)`＋D-11 WARNING 文字、`("bogus",true)`、`("",false)` 與計畫表逐項相符；`SERIALWRAP_CONSOLE_BACKEND` 只在 `native_console_kind()` 讀取；`tests/conftest.py:127`、`bb_harness.py:101` 已在隔離清單。
- **ConsoleMux POSIX**：waker 連呼叫只回一次 `Woken`、listener 回 `Acceptable`、write interest 切換、EAGAIN→`TimedOut`／`Ok(0)`、`MSG_NOSIGNAL`、跨 PAL handle 回 `Unsupported`，行為與計畫 Step 3.4 相符。
- **T11 schema**：`validate_rule_dict` 的檢查順序、`str()`／`int()`／truthiness、`$` 允許結尾 `\n`、`handler.exec=[]`＋`shell` 的 Python 怪癖（進入 `exec` 分支而非報錯）、大整數、unhashable `TypeError`、DF-08（contains 亦驗旗標）皆與 Python 差分一致；測試檔在紅燈到實作之間未被修改。
- **T0**：`behavior_changes.yaml` revision 5→6 為 add-only；cases.lock／cases.yaml 僅 `cases_revision` 行被改，其餘為新增；`freeze_slice_cases.py` 凍結 `slice_2.3.json`（分母 154）；`regen_lock.py --check`、`tools.contracts check` 通過；FenceReject 改名 `FsmMismatch` 等偏離均記於 commit 訊息。
- **平台 cfg**：僅在 `sw-pal`；Windows 本體以 `todo!` 骨架保留並可編譯（L3 討論清單完整性）。

## 逐 commit 結論

| Commit | 結論 | 說明 |
|---|---|---|
| `4b9d399` P6-T8 紅燈 | PASS | 計畫列 20 案齊備；紅燈 21 failed／4 綠的說明可信；`Capabilities.com0com_console` 骨架欄位合理 |
| `ccd90bd` P6-T8 實作 | PASS WITH FINDINGS | Linux 22/22；M1（與修訂後 #40／#64 矛盾）、L3、L4 |
| `984ea3f` P7-T0 | PASS WITH FINDINGS | 鎖／凍結 add-only 成立；M2（`AwaitDrain`）、M3（`p7_gate:py_parity`）、L5-5/6 |
| `1d013f8` P7-T1 golden | PASS | 用真實 Python 重擷取逐位元組相同（3.11、3.12）；L5-4 |
| `02faf78` P7 紅燈 | PASS | T11／T18A 計畫案例齊備，紅燈數與訊息一致 |
| `99c7452` P7-T11 | PASS WITH FINDINGS | 差分 5842 隨機 regex 0 差異；L1、L2 為邊角訊息差異 |
| `5af6c15` P7-T18A | PASS WITH FINDINGS | 預設位元組與 P3 相同；L5-1/2/3 |

## 發現摘要

| # | 嚴重度 | 位置 | 摘要 |
|---|---|---|---|
| M1 | Medium | `proc_scan/posix.rs:51-72`、`console_pty.rs:100` | `held_slave_paths` 用 readlink 字串，與修訂後 #40／#64（`same_tty` 身分）矛盾；缺 5 個 namespace 案 |
| M2 | Medium | `sw-core/src/recover.rs:17-18` | 骨架 `AwaitDrain` 與修訂 #57（`DrainBusy`）相反 |
| M3 | Medium | `984ea3f`（`l1_required.lock.json`） | 未建 `p7_gate:py_parity`／Python parity 測試入鎖（#111／#112） |
| L1 | Low | `schema.rs:148` | >4300 位數字字串 `int()` 訊息與 Python 不同 |
| L2 | Low | `pyre/parse.rs:509` | `(?P>x)` 錯誤訊息少一字元 |
| L3 | Low | `lib.rs:290`、Windows 骨架 | `com0com_console=true` 但本體 `todo!`；待辦清單未集中 |
| L4 | Low | `console_conformance.rs:62-80` | D-01 symlink 斷言對空暫存目錄恆真 |
| L5 | Low／資訊 | 多處 | T18A 未獨立 PR、時序案例 CI 穩定性、3.13 golden、SC-30 待 PA-OD-1、prereq exit 1 |
