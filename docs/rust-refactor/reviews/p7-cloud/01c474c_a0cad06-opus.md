# feature/rust-refactor-p7 01c474c..a0cad06 非實作者審查（Claude Opus）

## 範圍

- 分支 `origin/feature/rust-refactor-p7`，`01c474c..a0cad06`，共 5 個 commit，都不是 merge commit（舊到新）：
  - `040d0b3` feat(rust-event)：組裝 Event Engine 並接線 event RPC（P7 Task 16）
  - `0722f75` feat(rust-event)：接線 reader observer、BridgeQueries 與 Event Engine 啟停（P7 Task 16-wire）
  - `42d90f5` docs(rust-p7)：補 Task 16／16-wire 後 pytest 失敗清單證據
  - `3030adc` fix(rust-p7)：審查修正第 7 輪（截斷代理對的分塊解碼、Windows exit code 呈現、Windows 待辦判準、證據差異）
  - `a0cad06` docs(rust-p7)：補審查第 7 輪 pytest 失敗清單證據
- 本範圍沒有修改任何 `sw_core/**/*.py`。
- 前輪報告是 `p7-cloud/b65df2d_01c474c-opus.md`，其中兩個 Low（9c4a80e-1：Windows exit code；01c474c-1：README 漏列移除項）都在 3030adc 處理。核對結果見各節。

## 依據

- `docs/rust-refactor/serialwrap-rust-spec-final.md`、`owner-decisions-2026-10-15.md`（OD-14 起）、`owner-decisions-addenda.md`、`plans/parity-amendments-2026-10-26.md`
- `plans/08-slice23-login-events.md`：
  - Task 16（§2114 起）與其 parity 追加表
  - Task 16-wire（Step 1 測試表與「P7 修訂」）
  - SC-11 細化（§146：close_intake 在 drain 前、stop 與 drain 共用 5 s 期限、`unreaped` 非空時 exit 1）
- Python 基線：
  - `sw_core/service.py`：1393-1432 的 event 分支、`start`／`stop`（575-620）、`_engine_rx_observer`
  - `sw_core/event_engine/engine.py`
  - `sw_core/event_engine/event_log.py`：`tail` 以 `open(..., "r", encoding="utf-8").readlines()` 讀檔，即 strict 解碼
  - `sw_core/session_manager.py`：1499-1523，以及 `_on_bridge_rx`（4338 起）
- CPython 3.12 的 `TextIOWrapper` 與 `unicode_decode_utf8`。
  - 非 final 解碼時，若錯誤落在 `case 2`，且塊尾恰為 `0xED` 加上 `0xA0..0xBF`（截斷代理對），解碼器會 `goto End` 延後到下一塊。
  - `BufferedReader.read1(8192)` 在緩衝為空時直接向 raw 讀 n bytes，因此分塊大小固定為 8192，與 `st_blksize` 無關。
- `_winapi.GetExitCodeProcess` 以 `DWORD` 轉為不帶號 `int`。

## 方式與實跑結果

工具鏈為 `rust-toolchain.toml` 釘定的 1.96.1；獨立的 `CARGO_TARGET_DIR`，`-j2`。

| # | 命令或動作 | 結果 |
|---|---|---|
| 1 | `cargo test -p sw-core --features test-fakes --test {event_engine,event_store,rx_observer_wiring,event_dispatcher}` | 7／27／3／18 passed |
| 2 | `cargo test -p sw-daemon --features test-fakes --test {event_rpc,event_lifecycle,dispatch}`；`-p sw-pal --test handler_spawner` | 7／3／45／12 passed。event_lifecycle 第一次跑時 `shutdown_aborts_long_handler_within_budget` 在 `event_lifecycle.rs:140` 紅一次（`code != 0`；當時機器上還有另兩位審查者的 cargo 負載）。之後單跑 3 次、4 份並行、8 份並行加 8 條 busy loop，皆綠（見 0722f75-2） |
| 3 | 全 workspace：`cargo test --workspace --features sw-core/test-fakes,sw-pal/test-fakes,sw-daemon/test-fakes --no-fail-fast` | 只剩 `file_eacces_matches_golden`、`unix_fixture_rows_match_python` 兩筆，都是證據 README 已列的 root 環境類 |
| 4 | **截斷代理對 1:1 對照**：以 python3.12 產生 1452 個檔案（`b"x"*L + 尾序列 + 結尾`，L∈{8188..8193, 16380..16384}，22 種尾序列含 `ed a0`、`ed a0 80`、`ed bf bf`、`ed 9f bf`、`e0 80`、`f0 90`、`f4 90`、`c0`、`ff`…，6 種結尾），記錄 `readlines()` 的 `str(UnicodeDecodeError)`。暫存 cargo 測試以 `EventLogger::tail` 讀同一批檔案比對 | **1452／1452 逐字相同**（其中 1254 筆為錯誤訊息）。暫存測試已移除 |
| 5 | 反證：只還原 `event_log.rs` 到 3030adc^ | `tail_decode_error_position_is_chunk_relative` 紅（`case 5`，即新增的 case6）。#4 的對照也出現不一致，例如 `c00000`（8188×x＋`ed a0`、EOF）：Python 為 `position 0`，修前 Rust 為 `position 8188`。檔案已復原 |
| 6 | **event RPC 黑箱 A/B**：暫存黑箱測試對 Python daemon 與 Rust `serialwrapd`（`SERIALWRAP_AB_LABEL=rust-s21`）各送同一串 43 個 `event.*` 呼叫（見下表），去掉 `version` 與 `ts` 後比對 | **逐字相同**（JSON 檔 11342 bytes，`diff` 無輸出） |
| 7 | **事件端到端 A/B**：3 條規則（`command_output`＋`max_fires:2`、`spontaneous`＋`ALL`、`any`＋`kill -9 $$` 的 handler），`debug:true`，送 3 個 `command.submit`，再比對 `event.tail`、`rule_list`、`rule_get` | 23 筆事件的類型、順序、`matched_text`、`reason`、`exit_code`（3 與 -9）、`stdout_tail`／`stderr_tail`、`fire_count`、counter 都相同。唯一差異是 `wal_seq` 整體差 1，原因是兩邊 RX chunk 的切法不同。另外實測 WAL：Python 的開機 RX 分成兩筆，Rust 為一筆；兩邊的 `wal_seq` 都等於各自那個 RX chunk 的 WAL seq，符合 DF-15 |
| 8 | 反證：註解掉 `daemon_main.rs:1136` 的 `engine.close_intake();` | `event_lifecycle` 3 項**全綠**（見 0722f75-1）。檔案已復原 |
| 9 | 反證：把 `manager.add_rx_observer` 移到 `bootstrap_attach()` 之後、延遲 500 ms | `engine_started_before_bootstrap_attach` 紅。順序回歸有效，檔案已復原並重建 binary |
| 10 | `cargo check --workspace --tests --target x86_64-pc-windows-gnu`（含 test-fakes） | 沒有 error |
| 11 | `python3 scripts/rust/check_cfg_boundary.py` | rc 0；`py_returncode` 的 `#[cfg(windows)]` 位於 `crates/sw-pal/src/process.rs` |
| 12 | `python3 tests/acceptance/regen_lock.py --check --base 01c474c`；`git diff 01c474c..a0cad06 -- l1_required.lock.json` | 兩行 OK；刪除列 0、新增 21（14＋6＋1），21 個 nodeid 都實際存在並執行通過 |
| 13 | `pytest tests/test_slice21_scope.py tests/contract_blackbox/slice21/test_slice_scope.py`；後者另以 Rust daemon 跑（`rust-s21`） | Python：8 passed、2 skipped；Rust 輪：1 passed（`event.*` 已不回 METHOD_NOT_FOUND，`file.*` 仍回） |
| 14 | 證據檔：`wc -l`、`sha256sum`、`comm` 比對相鄰清單 | 0722f75 為 512 行、`f1fcb15f24ef4376`；3030adc 為 511 行、`5b1216e4c3e40aac`，都與 README 相符。9c4a80e→0722f75 為 +5／−1，0722f75→3030adc 為 +2／−3，與 README 自動段逐筆一致。檔案已排序去重，沒有家目錄路徑或憑證樣式 |
| 15 | 抽查新增失敗：`sm01`、`test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`、`sm04` 同範圍跑 2 次；`test_limits_queryable`、`test_newline_rejected`、`test_f12_version_reported_consistent`、`test_mgmt_stubs::…console_list` 跑 1 次 | 前者 3 passed ×2；後者 4 passed。`SESSION_NOT_READY-ATTACHING` 確實在起點清單內，與「環境類」判定一致 |

#6 的 43 個呼叫（Python 與 Rust 逐字相同，摘錄有代表性的列）：

| 輸入 | 兩邊相同的回應（去 `version`） |
|---|---|
| `event.com_enable {}`／`{"selector":0}`／`{"selector":7}` | `selector:""`、`""`、`"7"`，三者都是 `enabled:true`（`str(x or "")`） |
| `event.com_status {"selector":5}` | `{"ok":true,"selector":5,"enabled":false,"active_rules":[]}` |
| `event.com_status {"selector":["x"]}`／`{"a":1}` | `EXCEPTION`，`message` 為 `unhashable type: 'list'`／`'dict'` |
| `event.rule_set` 名稱 `N!` | `INVALID_RULE_SCHEMA`，`error` 為 `name must match [a-z0-9-]{1,64}` |
| `event.rule_set` 帶 `cooldown_ms:null` | `EXCEPTION`：`int() argument must be a string, a bytes-like object or a real number, not 'NoneType'` |
| `event.rule_set` 帶 `ok:"spoof"` | `{"id":1,"ok":"spoof","schema_version":1,…}`（RPC-H-14） |
| `event.rule_list {"selector":["COM0"]}`／`{"owner":5}` | 只回 `ALL` 規則；`[]` |
| `event.rule_get {"rule_id":5}`；`event.rule_delete {"rule_id":0}` | `RULE_NOT_FOUND`；`deleted:false` |
| `event.reset {"rule_id":5}`／`{"rule_id":""}`／`{"rule_id":"o.zz","selector":"COM0"}`／`{"selector":["COM0"]}` | `cleared` 依序為 0／0／1／1 |
| `event.com_disable {"selector":"COMX"}`（從未啟用） | `active_rules:["o.m"]`，並記下 `counter_reset` 與 `com_disabled` |
| `event.tail {"n":"3"}`／`{"since_ts":"x"}`／`{"n":1.5}` | `EXCEPTION`，訊息依序為 `bad operand type for unary -: 'str'`、`'<' not supported between instances of 'int' and 'str'`、`slice indices must be integers or None or have an __index__ method` |
| `event.tail {"n":true}`／`{"n":0}` | 最後 1 筆／全部 |

## 結論

彙整者抽查：`crates/sw-core/src/event_engine/mod.rs:337` 的 `stop` 會自行呼叫 `close_intake()`，因此刪掉 `daemon_main.rs:1136` 只會讓 intake 在整段 drain 期間保持開啟，現有測試仍然全綠。0722f75 #1 的 Medium 成立。

High 0、Medium 2、Low 4。

**event RPC 與事件 payload**：外部可觀察行為經 #6（43 個呼叫）與 #7（端到端 23 筆事件）黑箱 A/B 驗證，與 Python 1:1。

**前輪兩個 Low 都已修好**：
- 截斷代理對（aaf2c1d-1）：以 1452 組 CPython 3.12 對照驗證逐字相同，還原修正後回歸確實轉紅。
- Windows exit code（9c4a80e-1）：改為不帶號 DWORD，cfg 收在 sw-pal，Windows 交叉編譯通過，原生執行如實列在 P7-LW-6，沒有假裝完成。

**凍結鎖**：只新增。

**兩個 Medium 都是 0722f75 的測試問題**：
- 計畫列為「修正前必 FAIL」的 R1-F2 停止序測試被替換，但替換說明宣稱的行為測試並不存在，實測移除 `close_intake` 後全綠。
- `shutdown_aborts_long_handler_within_budget` 的斷言比計畫寬鬆，而且沒有登記這項修訂。

### 040d0b3 feat(rust-event): 組裝 Event Engine 並接線 event RPC

NO FINDINGS。

核對過、沒有問題的部分：

- **10 個 `event.*` 分支**（`service.rs` 的 `event_rpc`）：與 `service.py:1393-1432` 一致，#6 已逐字對照。
  - `rule_set` 成功時回 `{"ok":true, **raw}`。
  - `RuleError::Schema|Value` 轉 `INVALID_RULE_SCHEMA`。
  - `Type`／`Io` 轉為 framing `EXCEPTION`，不帶 version。
  - `rule_delete`、`rule_get`、`com_enable`、`com_disable` 以 `first_str` 實作 `str(x or "")`。
  - 其餘參數原樣傳值，`null` 視同缺。
- **未掛上 engine 時**仍回 METHOD_NOT_FOUND（`event_methods_without_engine_are_method_not_found`）。
- **`EventEngine` 的語意**與 `engine.py` 逐行一致：
  - `start` 是 `rule_loaded` 先、`com_enabled` 後；`rule_set`／`reload` 在鎖內先寫 `com_enabled`，鎖外再寫 `rule_loaded`。
  - `reload` 的事件依 added → removed → changed 記錄，清單排序屬已核准的 RPC-H-13。
  - `com_disable` 先記 counter_reset 再記 com_disabled，原本未啟用也一樣。
  - `reset` 在 `rule_id` 非 str 或不合法時回 0，與 `valid_rule_id` 的 `isinstance` 檢查等價；合法時以 rule_id 優先，且不檢查規則是否存在。
  - `emit_fire` 在 dispatch 前設 exhausted（EVT-D-E1）。
- **計畫 Task 16 表的 15 列**：實作合併成 7＋7 個測試名，逐列核對過斷言都還在（含 engine 層的 `rule_list` ALL 分支）。
  - 唯一的形式差異是 `event_rpc_does_not_block_other_sessions`：計畫寫「10 MiB、每 MiB 50 ms」，實作改為一次 500 ms 延遲。意圖等價，不列為發現。

### 0722f75 feat(rust-event): 接線 reader observer、BridgeQueries 與 Event Engine 啟停

核對過、沒有問題的部分：

- **reader observer**：在 WAL append 之後、`rx.append` 與送 actor 之前，以 `wal_entry.seq` 依序呼叫，panic 會被吞掉（DF-15）。observer 清單在 bridge 開啟時取快照；`add_rx_observer` 在 initial scan 與 bootstrap 之前呼叫，explicit targets 也涵蓋到（#7 與 #9 實證）。
- **`BridgeQueries`**：`known_coms` 升冪，`profile_for` 回 `profile_name`。`active_cmd_id_for` 讀 SessionCore 每次 `handle` 結束時發布的 `foreground_busy`；#7 中 `command_output` 與 `spontaneous` 的分類與 Python 完全相同。
- **daemon 啟停順序**符合 §146。`engine.stop` 的 WARNING 確實寫到 stderr（以 `--nocapture` 實見 `event dispatcher shutdown 逾時，終止 1 個 handler`）。

1. [Medium] `crates/sw-daemon/tests/event_lifecycle.rs:1-5`（對應 `crates/sw-daemon/src/daemon_main.rs:1136`）
   - **問題**：計畫 Task 16-wire 把 `shutdown_closes_event_intake_before_drain` 列為「修正前必 FAIL」的 R1-F2 回歸。實作改不新增 trace，並在檔頭與 08「P7 修訂」宣稱「停止序改以行為斷言（close_intake 後不再 spawn、與 drain 共用期限的終止時間）」。但 daemon 層沒有任何測試斷言「close_intake 之後、drain 期間不再受理與 spawn」。
   - **實證（#8）**：註解掉 `daemon_main.rs:1136` 的 `engine.close_intake();` 後，event_lifecycle 3 項全綠。原因是 `engine.stop` 內部會補呼叫 close_intake，所以「drain 期間 matcher 仍在受理、仍可 spawn」的回歸完全偵測不到。engine 層的 `close_intake_stops_feed_and_stop_reports` 只驗 engine 自身，不驗 daemon 的呼叫時點。
   - **確切修法**：不必新增 trace 事件，在 `event_lifecycle.rs` 新增 `shutdown_closes_event_intake_before_drain_behavior`：
     - 規則 A 的 handler 為 `sleep 60`。
     - 規則 B 比對一行由 fixture 在 stop 後持續吐出的 RX，例如 FakeSerial 每 50 ms 吐一次 `__B__`；若現有 Fixture 沒有，就新增一個 test-fakes 變體。
     - 送 `daemon.stop` 後記下 events log 的行數 n0。
     - daemon 結束後斷言 n0 之後只出現規則 A 的 `fire_timeout`：不得有 `match_recorded`、`fire_started` 或 `event_dropped`，也不得有規則 B 的任何事件。
     - 先在還原 `close_intake` 的狀態下確認此測試轉紅，再以 `regen_lock.py --l1-add-only` 登記。

2. [Medium] `crates/sw-daemon/tests/event_lifecycle.rs:107-147`（`shutdown_aborts_long_handler_within_budget`）
   - **問題一：斷言比計畫寬鬆**。
     - 計畫的精確斷言是「daemon 在 5.1 s 內結束」，並且「stderr 有 `event dispatcher shutdown 逾時` WARNING」。
     - 實作把上限放寬為 `took < 5600 ms`（第 141 行），也完全沒有斷言 stderr 的 WARNING。
     - 這兩項偏離沒有寫進檔頭或 08「P7 修訂」，修訂只交代了 trace 一項。
   - **問題二：曾不穩定一次**。本次在共用負載下觀察到一次 `assert_eq!(code, 0, …)`（第 140 行）失敗：daemon 回 exit 1，推測是 `ABORT_GRACE` 只有 100 ms、`unreaped` 非空。測試沒有印出 daemon stderr（`shutdown incomplete: …`），失敗時無從判定原因。
   - **確切修法**：
     - 改為在子行程啟動 daemon binary，或讓 `DaemonHooks` 接一個 test-fakes 專用的 warn sink，然後：
       - 斷言 stderr 含 `event dispatcher shutdown 逾時，終止 1 個 handler`；
       - `code != 0` 時，在斷言訊息中附上 stderr 的 `shutdown incomplete:` 行。
     - 時間上限有兩個選擇：
       - 恢復計畫的 5.1 s，但從 `WatcherStopped` 之後、也就是 close_intake 的時點起算（trace hook 已有 `Stage::WatcherStopped` 可用）；
       - 或保留 5.6 s，並在 08 Task 16-wire「P7 修訂」寫明理由：從 `daemon.stop` 送出起算，會把 server stop 與 watcher stop 的時間算進來。

3. [Low] `docs/rust-refactor/reviews/p7-local-windows-todo.md`（P7-LW-3）、`crates/sw-daemon/tests/event_lifecycle.rs:107-162`
   - **問題**：本 commit 新增 4 個測試檔，`rx_observer_wiring`、`event_engine` 屬 sw-core，`event_rpc`、`event_lifecycle` 屬 sw-daemon；L1 鎖只登記在 `p7_gate:cargo-test-workspace/linux`。
     - LW-3 只寫「P7 新增 sw-core 測試」，沒有涵蓋 sw-daemon 的兩檔。
     - `event_lifecycle` 的 `shutdown_aborts_long_handler_within_budget` 使用 `/bin/sh` 與 `/proc`，但沒有 `#[cfg(unix)]`。在 Windows 原生執行時，`marker_alive` 恆為 false，`assert!(running)` 必紅，Windows gate 會出現與產品無關的紅燈。
   - **確切修法**：
     - 為該測試與 `marker_alive` 加 `#[cfg(unix)]`，另立 Windows 版（`cmd.exe /d /s /c "ping -n 60 127.0.0.1"` 搭配 Job 檢查），或在 LW 清單登記為待原生補齊。
     - LW-3 補列 `sw-daemon --test event_rpc`、`--test event_lifecycle`，判準寫明 `passed == 該檔 Windows 適用 #[test] 數` 且 `ignored == 0`。

### 42d90f5 docs(rust-p7): 補 Task 16／16-wire 後 pytest 失敗清單證據

NO FINDINGS。

- 本 commit 原本的 README 列漏記 `−test_bridge_smoke_sm01`，但同範圍的 3030adc 已補成「+5／−1」，並新增自動產生的逐列 +/− 段。
- 我以 `comm` 重算，與修正後的內容一致。
- 證據檔的行數與 sha256 也與 README 相符（#14）。

### 3030adc fix(rust-p7): 審查修正第 7 輪——截斷代理對的分塊解碼、Windows exit code 呈現、Windows 待辦判準與證據差異

核對過、沒有問題的部分：

- **`decode_text_chunked`**：新增的條件與 CPython `case 2` 的特例等價。
  - CPython 的特例：`consumed`、`s[0] == 0xED`、`end - s == 2`、`s[1]` 在 A0..BF。
  - Rust 的條件：`!last`、`e.start + 2 == data.len()`、`data[e.start] == 0xED`、`data[e.start + 1]` 在 A0..BF。
  - 1452 組 CPython 對照逐字相同（#4）；修前紅（#5）。
- **`py_returncode`**：在 Windows 為 `exit_code as u32`，與 `_winapi.GetExitCodeProcess` 的 DWORD 相同；POSIX 原值回傳，訊號終止時為 `-signum`，#7 實見 `-9`。cfg 只在 sw-pal（#11），可交叉編譯（#10）。
- **design 文件與 LW 清單**：`design-event-trigger.md` 改寫為與 Python 相同的呈現。LW-2 為 7 項，LW-4 為 27 項，兩者都與檔內 `#[test]` 數相符，且沒有 cfg 排除。LW-6 判準 `passed == 10` 與 Linux 的 12 項減去 2 個 `#[cfg(unix)]` 相符。Windows 原生項目都標為待辦，沒有假裝完成。
- **凍結鎖**：只新增 1 筆。

1. [Low] `crates/sw-pal/tests/handler_spawner.rs:3`
   - **問題**：檔頭寫「報告 `passed` 須等於本檔 `#[test]` 數 9」，但本 commit 新增 `py_returncode_matches_python_popen` 後，Windows 適用數為 10，與 P7-LW-6 的新判準 `passed == 10` 矛盾。照檔頭驗收的 Windows 執行者會判錯。
   - **確切修法**：改為「報告 `passed` 須等於 10（12 個 `#[test]` 扣除 2 個 `#[cfg(unix)]`）、`ignored==0`，判準以 p7-local-windows-todo P7-LW-6 為準」。

2. [Low] `crates/sw-core/src/event_engine/dispatcher.rs:262-266`
   - **問題**：新增的回歸只驗 sw-pal helper（`py_returncode(-1073741819) == 3221225477`），沒有任何測試把 dispatcher 的 `fire_completed.exit_code` 與 helper 綁在一起。
     - 把這幾行還原成 `i64::from(exit_code)` 後，Linux 上 helper 是恆等函式，所以全綠。
     - 日後在 LW-6 做 Windows 原生執行時，helper 測試同樣會綠。
     - 也就是說，9c4a80e-1 真正的外部可觀察行為（事件 payload）沒有回歸保護。
   - **確切修法**：在 `crates/sw-core/tests/event_dispatcher.rs` 新增 `#[cfg(windows)] fire_completed_exit_code_is_unsigned_dword`。
     - 以 `FakeHandlerSpawner::scripted` 回 `HandlerOutcome { exit_code: -1073741819, .. }`。
     - 斷言 emit 的 `fire_completed` 中 `exit_code` 為 `PyInt::Small(3221225477)`。
     - 若想在 Linux 上也有保護，可加一個 `#[cfg(not(windows))]` 案例斷言 `-9 → -9`。
     - 以 add-only 登記到 windows 集合，並併入 P7-LW-3。

### a0cad06 docs(rust-p7): 補審查第 7 輪 pytest 失敗清單證據

核對過：`pytest-fail-3030adc.txt` 為 511 行、sha256 前 16 碼 `5b1216e4c3e40aac`，與 README 相符；相對 0722f75 為 +2／−3，與 `comm` 一致；檔案已排序去重。

1. [Low] `docs/rust-refactor/reviews/p7-evidence/README.md`（`pytest-fail-3030adc.txt` 列）
   - **問題**：本輪新增的 `+test_bridge_smoke_sm01` 被判為「DeviceHeldError 環境類」，但這一輪沒有附上 CLAUDE.md 要求的「同範圍連跑兩次、失敗集合不同」實測。前幾輪（d5b2dd6、c58e8d4）都附了重跑紀錄，這一輪只沿用舊判定。我在本機同範圍跑 2 次都是 3 passed（#15），結果與環境類判定一致，但證據應該落在樹內。
   - **確切修法**：在該列後補一段實跑紀錄，例如 `python3.12 -m pytest -q -p no:cacheprovider tests/test_functest_bridge_smoke.py::test_bridge_smoke_sm01 "tests/contract_blackbox/slice21/test_safety_core.py::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]"` 跑兩次的結果，並附上失敗時的 `ATTACH_FAILED:DeviceHeldError` 摘錄。

## High／Medium 摘要

| 等級 | commit | 位置 | 摘要 |
|---|---|---|---|
| Medium | 0722f75 | `crates/sw-daemon/tests/event_lifecycle.rs:1-5`／`daemon_main.rs:1136` | 計畫「修正前必 FAIL」的 R1-F2 停止序回歸被替換，但替換說明宣稱的行為測試不存在；移除 `engine.close_intake()` 後 event_lifecycle 仍全綠 |
| Medium | 0722f75 | `crates/sw-daemon/tests/event_lifecycle.rs:107-147` | `shutdown_aborts_long_handler_within_budget` 的上限從計畫的 5.1 s 放寬為 5.6 s，且漏了 stderr WARNING 斷言，都未登記修訂；共用負載下曾因 exit 1 紅一次，且沒有印出 stderr |

High：0。

Low（4）：
- 0722f75#3：sw-daemon 新測試未列入 Windows 待辦，`event_lifecycle` 的 POSIX-only 測試沒有加 cfg。
- 3030adc#1：`handler_spawner.rs` 檔頭的 Windows 判準仍寫 9，與 LW-6 的 10 矛盾。
- 3030adc#2：dispatcher 的 `exit_code` emit 沒有回歸綁定 `py_returncode`。
- a0cad06#1：本輪新增的環境類失敗沒有同範圍重跑紀錄。
