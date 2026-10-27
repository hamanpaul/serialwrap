# feature/rust-refactor-p7 06c61d0..b65df2d 非實作者審查（Claude Opus）

## 範圍

- 工作樹：`/home/user/rr-p7`（detached `origin/feature/rust-refactor-p7` = `b65df2d`）
- 範圍：`06c61d0..b65df2d`，4 個 commit，皆非 merge commit
  - `ae82afe` fix(rust-p7)：審查修正第 4 輪（重掛 quiet 排到期、自動重探依實際結果收斂、例外只回覆發起者、busid 懸空連結）
  - `98cc983` docs(rust-p7)：補審查第 4 輪 pytest 失敗清單證據
  - `aaf2c1d` fix(rust-event)：審查修正第 5 輪（容器 NaN identity、Windows 規則檔 CRLF、tail 解碼位置分塊、計畫修訂與證據）
  - `b65df2d` docs(rust-p7)：補審查第 5 輪 pytest 失敗清單證據
- 本範圍沒有修改任何 `sw_core/**/*.py`。

## 依據

- `/home/user/rr-p7/CLAUDE.md`（測試政策、「已知 flaky 不得當放行理由」）
- `docs/rust-refactor/serialwrap-rust-spec-final.md`、`docs/rust-refactor/owner-decisions-2026-10-15.md`（OD-14 起）
- `/home/user/pa`（docs/parity-amendments，`d763ee5`）的 `plans/08-slice23-login-events.md`（Task 4／5／6／7／13）與 `parity-amendments-2026-10-26.md`
- Python 基線：`sw_core/event_engine/{event_log,registry,counter}.py`、`sw_core/session_manager.py`（`_probe_existing_bridge` :4426-4520、probe lock :2182／:3688／:7586）、`sw_core/transport_stall.py:72-91`、`sw_core/rpc_posix.py:28`
- 前輪報告：`p7-cloud/92e0223_76de0cf-opus.md`（第 4 輪來源）、`p7-cloud/76de0cf_96b28dd-opus.md`（第 5 輪來源）、`p7-cloud/96b28dd_06c61d0-opus.md`（ENOEXEC Medium）

## 實跑驗證命令與結果

| 命令 | 結果 |
|---|---|
| `CARGO_TARGET_DIR=/home/user/target-p7 cargo test -j4 --workspace --features test-fakes --no-fail-fast -- --test-threads=4` | 137 個 binary，1920 passed／3 failed：`file_eacces_matches_golden`、`unix_fixture_rows_match_python`（以 root 執行，EACCES 不成立，前輪已記載）、`lock_conformance_suite`（`stale_socket_reclaimed: AlreadyRunning`，只在高併發全套中出現；單獨連跑 3/3 通過，且本範圍未修改 lock 相關檔案） |
| `python3 scripts/rust/check_cfg_boundary.py` | exit 0。新增的 `#[cfg(windows)] TEXT_NEWLINE` 在 `crates/sw-pal/src/fs_ops.rs`，位置合規 |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0，只有 dead-code warning |
| `python3 scripts/rust/check_p7_prereqs.py` | `[check_p7_prereqs] OK` |
| `cargo fmt --check` | 無法用釘定工具鏈驗證：1.96.1 沒有安裝 rustfmt。改用 `+stable` 跑，會在全 repo 約 160 個檔案報差異，大多不在本範圍，屬工具版本差異，不列為發現 |
| 還原 `pyops.rs` 的修正 | `diff_against_nan_field_is_unchanged`、`tail_filter_list_with_nan_matches_python` 紅 |
| 還原 `event_log.rs` 的修正 | `tail_decode_error_position_is_chunk_relative` 紅 |
| 還原 `usb_posix.rs` 的修正 | `busid_follows_dangling_device_symlink` 紅 |
| 還原 `session_core.rs` hunk 0（adopt 排 BootQuietExpired） | `reattach_within_quiet_fence_expires_with_window` 紅 |
| 還原 `session_core.rs` hunk 1（例外只回第一個 waiter） | `manual_reprobe_exception_only_fails_first_waiter_others_reprobe` 紅 |
| 還原 `session_core.rs` hunk 2（刪除 probe_refused 覆寫） | `quiet_arm_during_auto_reprobe_ready_result_is_ready`、`…_not_ready_keeps_failure_code` 紅 |
| 突變 M9（`reprobe_tick` 中 `probe_busy` 時提早 return） | `reprobe_tick_while_manual_probe_busy_advances_next_reprobe_at` 紅 |
| 每次還原或突變之後 `git checkout .`、`git status --short` | 皆乾淨 |
| CPython 3.12.3 探針（`EventLogger.tail`）：NaN 過濾、分塊解碼共 16 組 | 見 aaf2c1d 節。15 組與 Rust 一致，1 組不一致（L-1） |
| CPython 3.12.3 `os.path.realpath` 對照 Rust `SysfsUsbTopology`，8 組結構（懸空、絕對懸空、迴圈、`..` 越界、鏈式、缺段後 `..`、穿過一般檔、`.`／`//`） | 8/8 busid 一致 |
| `python3.12 -m pytest tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready` ×2 | 2/2 passed |
| `python3.12 -m pytest tests/test_rust_goldens_fresh.py tests/test_event_replace_df18.py` | 21 passed |
| 證據檔：`wc -l`、`sha256sum | cut -c1-16`、`comm` 比對 | 見 98cc983／b65df2d 節 |

---

### ae82afe fix(rust-p7): 審查修正第 4 輪——重掛 quiet 排到期、自動重探依實際結果收斂、例外只回覆發起者、busid 懸空連結

NO FINDINGS

逐項核對：

- **bea2d8e-1（重掛 quiet 排到期）**：`session_core.rs:2276-2285`。只要 quiet 生效，就以新 epoch 排 `BootQuietExpired`。`timer()`（:2763）要求 `conn_epoch == epoch` 且 `boot_quiet_until <= now`，舊 epoch 的計時器與 re-arm 前的計時器都會被正確忽略。passthrough 已先清掉 quiet，所以不會誤排。這與 Python 用 `time.monotonic() < self_test_quiet_until` 判定的行為一致。還原後回歸測試轉紅。
- **d5b2dd6-1（自動重探依實際結果收斂）**：Python `_probe_existing_bridge`（:4457-4520）只在 probe 前檢查一次 `_boot_quiet_blocks_probe`。probe 結束後 `ok` 為真就設 READY（#139 註解明載），不看 login_gen。Rust 刪除覆寫後，run 只比對自身的 `(epoch, fsm_gen)`，結果原樣交給 `auto_reprobe_converge`。Ready 與 NotReady 兩個變體都會在還原後轉紅，期望值（READY／`last_error` Null／`reprobe_attempts` 0；ATTACHED／`READY_NONCE_TIMEOUT`／1）與 Python 一致。
- **bea2d8e-2（例外只回覆發起者）**：Python 手動 attach／recover 以 `with probe_lock:` 依序各自 probe（:3688-3690、:7586-7588），所以第二個呼叫不會連帶收到例外。Rust 只回 `Err` 給第一個 waiter，其餘走 `reprobe_existing_bridge` 以新世代重派，語意相符。
- **d5b2dd6-3（L-3）**：突變 M9 下測試確實轉紅。
- **d5b2dd6-4（busid 懸空連結）**：`join_realpath` 與 CPython 3.12 `posixpath._joinrealpath` 逐行對照，包括 `partition`、`..` 處理、`seen` 快取，以及迴圈時回傳 `join(newpath, rest), False`。8 組實測結構的 busid 全數與 CPython 一致。殘留的極端差異有兩個：(a) `readlink` 在 lstat 之後才失敗的競態，CPython 會拋例外再被外層 `except` 轉成 `None`，Rust 則繼續展開；(b) 迴圈分支沒有套用最後的 `abspath()`（normpath）。兩者都只在 sysfs 出現迴圈或競態時才會發生，不列為發現。
- `login_quiet.rs:1002` 由 `assert_eq!(…, "WalUnavailableError")` 放寬為 `is_err()`。這是前輪 bea2d8e-3 要求的修改：Python 的 EXCEPTION message 是 `str(exc)` 而非型別名，此偏離已在 p7-fix-responses 第 2／4 輪登記，由 Task 7 `LoginReport` 收斂（計畫 Task 7 仍未完成）。不算削弱斷言。
- L1 鎖：本 commit add-only 新增 6 筆，每個名稱都對應到實際存在的測試。

### 98cc983 docs(rust-p7): 補審查第 4 輪 pytest 失敗清單證據

NO FINDINGS

- `pytest-fail-ae82afe.txt`：510 行，sha256 前 16 位 `a12acbf50459427d`，與 README:14 相符。
- `comm` 相對 `pytest-fail-91109e7.txt`：+2／−0。新增兩筆為 `test_mgmt_stubs::test_live_daemon_fixture_serves_attached_console_list`（ERROR）與 `test_safety_core::test_reject_paths_zero_tx[SESSION_NOT_READY-ATTACHING]`，與 README 一致。後者確實在 `pytest-fail-start-de24500.txt` 內。兩者都是 Python daemon 黑箱測試，本 commit 群組沒有修改 `sw_core/`。

### aaf2c1d fix(rust-event): 審查修正第 5 輪——容器 NaN identity、Windows 規則檔 CRLF、tail 解碼位置分塊、計畫修訂與證據

1. **[Low] `crates/sw-core/src/event_engine/event_log.rs:229` — 分塊解碼漏了 CPython 對「截斷代理對」的特例緩衝**
   - 問題：`decode_text_chunked` 只在 `e.reason == "unexpected end of data"` 時把塊尾留到下一塊。CPython `unicode_decode_utf8` 在非 final 模式有一個特例（"Truncated surrogate code in range D800-DFFF"）：塊尾剛好剩 2 bytes、第一個是 `0xED`、第二個在 `0xA0..=0xBF` 時，不報錯而是緩衝到下一塊。結果是錯誤位置相對於下一塊，而 Rust 以本塊計算。
   - 重現（CPython 3.12.3 `EventLogger.tail()` 對照 Rust `EventLogger::tail`）：
     - 檔案內容 `b"x"*8190 + b"\xed\xa0\x80\n"`
       - Python：`'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte`
       - Rust：`'utf-8' codec can't decode byte 0xed in position 8190: invalid continuation byte`
     - 對照組都一致：`x*8190+\xf4\x90\x80`、`x*8190+\xe0\x80\x80`、`x*8191+\xc0\xaf`、`x*8191+\xf0\x9f\x98`（EOF）、`x*8191+\xf0\x9f+x*8192+\xff`、`x*16383+中+\xff`、`\r*8192+\n\xff`，以及測試內 5 組。
   - 確切修法：在 `!last` 分支，除了 `"unexpected end of data"` 之外，再加一個條件：`e.start + 2 == data.len() && data[e.start] == 0xED && (0xA0..=0xBF).contains(&data[e.start + 1])` 時同樣以 `carry = data[e.start..]` 延後。`tail_decode_error_position_is_chunk_relative` 補上上述案例，期望值用 CPython 實測的 `position 0`。
   - 嚴重度理由：只影響錯誤訊息的 position，並且需要代理對編碼的位元組剛好跨在 8192 邊界上。

2. **[Low] `docs/rust-refactor/reviews/p7-local-windows-todo.md:18`／`:20` — CRLF 修正的 Windows 原生核對沒有實際登記**
   - 問題：p7-fix-responses 的 c58e8d4-2 寫「Windows 原生核對併入 P7-LW-4」，但 LW-4 列仍寫「`event_store`（23 項）」，實際已有 27 項，也沒提 CRLF。新增的 `fs_ops_p7::create_truncate_text_uses_platform_newline` 屬於 LW-2，LW-2 列仍寫「`fs_ops_p7`（6 項）」，實際已有 7 項，判準也只是 `passed >= 1`。在 Linux 上這個修正是 no-op（`TEXT_NEWLINE == "\n"`），回歸測試無法在本機轉紅，Windows 分支目前只通過 `cargo check`。沒有假裝已完成，但追溯點不完整。
   - 確切修法：LW-2 改為 `fs_ops_p7`（7 項，含 `create_truncate_text_uses_platform_newline`，必須在 Windows 斷言 `TEXT_NEWLINE == "\r\n"`）。LW-4 改為 `event_store`（27 項，含 `upsert_rule_file_indent2_sorted_no_newline` 的 CRLF 期望值），判準改為 `passed == 總數` 且 `ignored == 0`。

其餘逐項核對：

- **c58e8d4-1（NaN identity）**：Python 的 `json.loads`（含 `rpc_posix.py:28` 的 RPC 參數）都經 `_CONSTANTS.__getitem__` 取得同一個 `NaN` 物件，`dict_equal`／`list_richcompare` 以 `PyObject_RichCompareBool`（identity 捷徑）比較元素，頂層 `!=` 沒有捷徑。`py_eq_elem`（pyops.rs:85）只在容器元素層把兩個 NaN 視為相等，list `<` 找第一個不等元素時也用它。實測一致的案例：`[NaN]`→1、`NaN`→0、`{"a":NaN}`→1、`[{"a":NaN}]`→0、`[[NaN]]`→1、`since_ts=[[NaN],2]`→1、`diff_against` 含 NaN 欄位→`changed=[]`。還原後兩個回歸測試轉紅。
- **c58e8d4-2（CRLF）**：Python `registry.py:105`、`counter.py:53` 用文字模式 `open(tmp, "w")` 寫入，`event_log.py:36` 用 `"ab"`。Rust 只把前兩者改成 `create_truncate_text`，event log 維持位元組寫入，對應正確。`create_truncate_text` 是 FsOps trait 的 add-only 預設方法，posix／windows 都已實作 `create_truncate`。
- **c58e8d4-3**：讀取端只有 `tail` 走 TextIOWrapper 分塊路徑（`readlines()`）。counter／registry 的 `json.load(f)` 是 `f.read()` 整檔一次 final 解碼，所以 Rust 保留 `read_text_utf8`，這個選擇正確。`universal_newlines` 在解碼之後才套用，與 IncrementalNewlineDecoder 的結果相同。
- 計畫：08 Task 13／15 加了「P7 修訂」區塊。L1 鎖 add-only 新增 5 筆，與 commit message 相符。`tail_reads_current_file_only` 與既有測試並存，並非改名。
- `red-runs.md`：「審查第 5 輪」列已由本次還原實驗驗證（3 項紅）。

### b65df2d docs(rust-p7): 補審查第 5 輪 pytest 失敗清單證據

1. **[Low] `docs/rust-refactor/reviews/p7-evidence/README.md:15` — 差異說明只列新增，漏了移除項**
   - 問題：`comm` 相對 `pytest-fail-ae82afe.txt` 實際是 **+1／−3**。新增 `test_bb_daemon_e2e::test_session_reaches_ready`，移除 `test_mgmt_stubs::test_live_daemon_fixture_serves_attached_console_list`、`test_safety_core[SESSION_NOT_READY-ATTACHING]`、`test_l15_smoke::test_reject_zero_tx_runs_on_python`。README 只寫了 +1。前兩輪（76de0cf-1、96b28dd-1）已要求寫出完整的 +/−。
   - 確切修法：README:15 改為「相對 ae82afe：+1／−3」，並列出三個移除項。
- 其他項目相符：508 行，sha256 前 16 位 `031452ef72aa78c7`。相對 91109e7 唯一新增的就是 `test_session_reaches_ready`。這是 Python daemon 黑箱測試，本範圍沒有修改 `sw_core/`。本次同範圍重跑 2/2 通過，與 README「集合不同」的宣稱一致。

---

## 前輪未結（不計入本範圍）

- `96b28dd_06c61d0-opus.md` 的 **Medium（ENOEXEC）** 仍未處理：`crates/sw-pal/src/process/handler_posix.rs` 在本範圍沒有任何變更，p7-fix-responses 也還沒有對應的處理列。合併前必須補上修正（`pre_exec` 內自行走 PATH 並呼叫 `execve`，再補 conformance 回歸測試），或補上 deferred 理由。

## High／Medium 摘要

| 嚴重度 | commit | 位置 | 摘要 |
|---|---|---|---|
| — | — | — | 本範圍無 High、無 Medium |

統計：High 0、Medium 0、Low 3（aaf2c1d-1 截斷代理對的分塊位置、aaf2c1d-2 Windows 待辦項目數與判準未更新、b65df2d-1 README 漏列 −3）。
