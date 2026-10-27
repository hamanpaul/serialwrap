# feature/rust-refactor-p7 b65df2d..01c474c 非實作者審查（Claude Opus）

## 範圍

- 分支 `origin/feature/rust-refactor-p7`，`b65df2d..01c474c`，共 2 個 commit，都不是 merge commit：
  - `9c4a80e` fix(rust-p7)：審查修正第 6 輪
  - `01c474c` docs(rust-p7)：補審查第 6 輪的 pytest 失敗清單證據
- 本範圍處理前輪報告 `p7-cloud/96b28dd_06c61d0-opus.md` 的發現：fa7e409 的 ENOEXEC Medium 與 4 個 Low，以及 91109e7 的 3 個 Low。
- 本範圍沒有修改任何 `sw_core/**/*.py`。

## 依據

- `docs/rust-refactor/serialwrap-rust-spec-final.md`
- `owner-decisions-2026-10-15.md`（OD-14 起）
- docs/parity-amendments 的 `plans/08-slice23-login-events.md` 與 `parity-amendments-2026-10-26.md`
- Python 基線 `sw_core/event_engine/dispatcher.py`。`_run_handler` 以 `subprocess.Popen(argv, env=env, close_fds=True, start_new_session=True)` 啟動 handler，`OSError` 轉為 `fire_failed`，`stop` 則以 `shutdown(wait=True)` 結束。
- CPython 3.12 的 `subprocess._execute_child` 與 `_posixsubprocess.child_exec`

## 方式與實跑結果

| # | 命令或動作 | 結果 |
|---|---|---|
| 1 | `cargo test -p sw-pal --features test-fakes --test handler_spawner` | 11 passed |
| 2 | `cargo test -p sw-core --features test-fakes --test event_dispatcher` | 18 passed |
| 3 | 反證：只還原 `handler_posix.rs` 的修正 | `native_spawn_enoexec_is_python_like`、`native_env_order_is_spec_order` 紅（9 passed、2 failed） |
| 4 | 反證：只還原 `dispatcher.rs` 的修正 | `stop_aborts_handler_registered_after_deadline` 紅（17 passed、1 failed） |
| 5 | 1:1 對照：同一組 10 個輸入，先在 CPython 3.12 以 `Popen(..., env=..., close_fds=True, start_new_session=True)` 執行，再在 scratch 測試以 `sw_pal::native().handlers.run` 執行（暫時檔用後已刪，沒有提交） | 逐項相同，見下表 |
| 6 | `cargo check -p sw-pal -p sw-core --tests --target x86_64-pc-windows-gnu` | 沒有 error，`WIN_SPAWN_LOCK` 的 Windows 兩處可以編譯 |
| 7 | `python3 scripts/rust/check_cfg_boundary.py` | rc 0 |
| 8 | `regen_lock.py --check --base b65df2d`；比對 `git diff` 的刪除列 | 兩行 OK；刪除列為 0，只新增 3 筆 |
| 9 | `pytest -q tests/test_l15_smoke.py::test_reject_zero_tx_runs_on_python` | 本機 1 passed。證據稱這支在作者機器上失敗、屬環境因素（`DeviceHeldError`），本機無法重現該失敗 |

#5 的 1:1 對照表。Python 與 Rust 兩欄逐字相同；`<D>` 為暫存目錄，內含 0755、沒有 shebang 的 `noshebang`，以及一個子目錄 `dirx`：

| argv | env | Python ＝ Rust |
|---|---|---|
| `[<D>/noshebang]` | `PATH=/usr/bin:/bin` | `[Errno 8] Exec format error: '<D>/noshebang'` |
| `[noshebang]` | `PATH=/nonexist:<D>` | `[Errno 8] Exec format error: 'noshebang'` |
| `[sw-p7-nonexistent-binary]` | `{}` | `[Errno 2] No such file or directory: 'sw-p7-nonexistent-binary'` |
| `[sh,-c,exit 5]` | `{}`（defpath） | completed、exit 5 |
| `[noshebang]` | `PATH=/etc/passwd:<D>`（ENOTDIR 後接 ENOEXEC） | `[Errno 8] Exec format error: 'noshebang'` |
| `[dirx]` | `PATH=<D>` | `[Errno 13] Permission denied: 'dirx'` |
| `[noshebang,x]` | `PATH=`（空字串，相對 cwd） | `[Errno 2] No such file or directory: 'noshebang'` |
| `[nothere/x]` | `{}` | `[Errno 2] No such file or directory: 'nothere/x'` |
| `[/bin/sh,-c,cat /proc/$$/environ]` | `B=1,A=2,PATH=/bin` | `B=1\0A=2\0PATH=/bin\0`（保留插入序） |

## 結論

High 0、Medium 0、Low 2。前輪的 ENOEXEC Medium 已依 `_posixsubprocess` 語意修正，並以實測確認與 Python 1:1。

### 9c4a80e fix(rust-p7): 審查修正第 6 輪——POSIX handler 自行 execve（無 ENOEXEC 後備）、env 依序、dispatcher 逾時後登記即 abort、Windows spawn 互斥

核對過、沒有問題的部分：

- **`ExecPlan` 的路徑查找**：與 CPython 3.12 `_execute_child` 一致。
  - `argv0` 含 `/` 時只試它本身；這與 `os.path.dirname(executable)` 非空等價。
  - 否則依 `spec.env` 的 `PATH` 組候選，沒有 `PATH` 時用 `os.defpath`＝`/bin:/usr/bin`。
  - 空目錄項會變成相對路徑，與 `os.path.join(b'', exe)` 相同。
- **`ExecPlan` 的錯誤選擇**：「取第一個非 ENOENT／ENOTDIR 的 errno，否則取最後一個」，與 `child_exec` 的 `saved_errno` 規則相同。
- **錯誤訊息**：檔名使用原始的 `argv0`，與 `err_filename = orig_executable` 相同。
- **`pre_exec` 內的安全性**：只呼叫 setsid、fcntl、execve 與 errno 讀取，不配置記憶體。指標指向 fork 前已建好的 CString 堆積緩衝，`ExecPlan` 被 move 進 closure 後緩衝位址不變。
- **為何 std 不會再做 `execvp`**：有 `pre_exec` 時 std 走 fork 路徑，不走 posix_spawn；closure 一律回 `Err`，所以 std 自己的 `execvp` 永遠不會執行，錯誤由 std 的 CLOEXEC pipe 回報給父行程。
- **嵌入 NUL**：仍走 `spawn_error` 的 `InvalidInput → "embedded null byte"` 分支。dispatcher 依此對應 Python 的 `ValueError` 路徑（不 emit），原有回歸 `spawn_value_error_emits_nothing_and_releases_rule` 仍綠。
- **env 依序**：`build_env` 以 `set` 去重。實測同名鍵重複時 envp 兩筆都會保留，由子行程取最後一筆；不過 `build_env` 在正式路徑上不會產生重複鍵，所以沒有可達的差異。
- **dispatcher 的 `aborting`**：與 `running` 的登記在同一把鎖下檢查，旗標在 stop 逾時後、觸發既有 abort 的同一臨界區內設立，沒有競態窗口。`closed` 本來就是永久狀態，`aborting` 不重設不影響重啟語意。bounded stop 本身是前輪已接受的 SC-11 細化。新回歸用 condvar 閘門讓 worker 停在 `counters.load`，時序是決定性的。
- **`stop_deadline_aborts_running_handlers`**：上限改回 300 ms，是收緊。`ignore_abort` 案多給的 100 ms 有寫明理由。
- **`WIN_SPAWN_LOCK`**：只在 sw-pal 內使用。handler 的持鎖範圍是從建立 pipe 到關閉子端，所有提前 `?` 返回時 guard 都會自動釋放。ProcessCtl 只在 `CreateProcessW` 的區段持鎖。p7 分支上沒有其他鎖，所以沒有鎖順序問題。殘留項已如實登記在 P7-LW-6，Windows 原生項目沒有假裝完成。
- **凍結鎖**：只新增 3 筆（#8）。

1. [Low] `docs/design-event-trigger.md:246-247` — 問題：前輪 fa7e409-4 被當成「documented」結案。但 Windows 的 `fire_completed.exit_code` 以有號 i32 呈現，例如 NTSTATUS `0xC0000005` 會呈現為 `-1073741819`；Python 的 `returncode` 是不帶號 DWORD，呈現為 `3221225477`。這是外部可觀察的差異，屬於擁有者要求的功能面 1:1 範圍。前輪雖然列了「註明」與「還原」兩個選項，但只寫進設計文件，並沒有對應的 OD 或 behavior_changes 授權。另外，`HandlerOutcome.exit_code: i32` 雖然凍結，修正並不需要改它。 — 確切修法（add-only，不改凍結介面）：在 sw-pal 新增 `pub fn py_returncode(exit_code: i32) -> i64`。這個函式放在 `process.rs`，以平台分檔實作：Windows 為 `exit_code as u32 as i64`，POSIX 原值回傳。dispatcher emit `exit_code` 時改呼叫它，並補一支 Windows 單元測試，斷言 `-1073741819 → 3221225477`。若擁有者選擇保留有號呈現，請改為補一條逐項 OD，並在 design 文件引用該 OD。

### 01c474c docs(rust-p7): 補審查第 6 輪 pytest 失敗清單證據

- 證據檔 `pytest-fail-9c4a80e.txt` 共 508 行，sha256 前 16 碼 `5dca7ec693660c98`，與 README 列的值相符；檔內沒有家目錄路徑或憑證樣式。

1. [Low] `docs/rust-refactor/reviews/p7-evidence/README.md`（`pytest-fail-9c4a80e.txt` 列） — 問題：README 寫「相對 aaf2c1d：+`test_l15_smoke::test_reject_zero_tx_runs_on_python`」，但實際 `diff` 兩檔，另外還少了 `−tests/contract_blackbox/test_bb_daemon_e2e.py::test_session_reaches_ready`。兩檔行數都是 508，正是一增一減的結果。這與前輪 96b28dd-1 指出的「差異說明不完整」是同一類問題。 — 確切修法：該列改為「+`test_l15_smoke::test_reject_zero_tx_runs_on_python`、−`test_bb_daemon_e2e::test_session_reaches_ready`（上一輪已記為同範圍兩次重跑、集合不同）」。

## High／Medium 摘要

| 等級 | commit | 位置 | 摘要 |
|---|---|---|---|
| — | — | — | 本範圍沒有 High 或 Medium |

High：0。Medium：0。

Low（2）：
- 9c4a80e#1：Windows exit code 以有號呈現，與 Python 的 DWORD 不一致，且沒有擁有者授權。
- 01c474c#1：證據 README 的差異說明漏列 1 筆移除。
