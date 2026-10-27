## 審查結果：8a7986e..edfc741（feature/rust-refactor-p5，三個 commit）

**結論：沒有 High 級發現，有 7 個 Medium、3 個 Low。** Medium 裡有 5 個是與 Python 行為不一致或證據不實，另 2 個是測試蓋不到風險、計畫項目未完成。

審查期間沒有修改 worktree 內任何檔案。驗證有兩條：用同一組 env 分別起 `python3 serialwrapd.py` 與 `target/debug/serialwrapd` 對照；把原始碼複製到 `/tmp/rv`（target 另放 `/tmp/rv-target`）跑一個修改後的測試。

實跑結果（非 root，uid 1000）：
- `cargo test -p sw-daemon --features test-fakes --test daemon_process --test restart_after_commit`：daemon_process **27 passed／1 failed**，失敗的是 `wal_fault_after_ready_splits_agent_and_auto_tx`（見 3938032 第 4 點）。cargo 遇到這個失敗後停止，所以 `restart_after_commit` 在本機**沒有執行**。
- `cargo test -p sw-core --features test-fakes --test session_manager`：43 passed。
- `pytest tests/test_slice21_skeleton_guards.py tests/test_daemon_lock.py`：8 passed。
- `check_cfg_boundary` rc=0；三個 commit 新增的 `#[cfg]` 都是 `feature = "test-fakes"` 或 `test`，沒有平台 cfg 跑出 `crates/sw-pal`。
- L1 鎖的 diff 只有 `+` 行，確認為 add-only。

### 3938032 feat(sw-daemon): serialwrapd 啟動順序、有序關機與 exit code

1. **[Medium] `crates/sw-daemon/src/daemon_main.rs:907` — 後端 env 檢查做得比 Python 早，exit code 不同。**
   - 問題：`check_backend_env` 在 argv 解析和 SC-19 allowlist 判定之前就執行。Python 要到 `_build_lock`／`_build_server`／`select_device_backend()` 才檢查後端，那時 argparse、allowlist、`ensure_runtime_dirs`、`load_profiles` 都已經跑過。實測對照：

     | 輸入 | Python | Rust |
     |---|---|---|
     | `SERIALWRAP_RPC_BACKEND=bogus serialwrapd --bogus` | rc=2，argparse usage | rc=1，`unsupported backend: 'bogus'` |
     | `SERIALWRAP_LOCK_BACKEND=bogus`＋非隔離＋allowlist | rc=2，`TEST_DEVICE_ALLOWLIST_REJECTED…` | rc=1 |
     | `SERIALWRAP_DEVICE_BACKEND=bogus` | 先建好 cfg／state／run／wal 目錄、印 `no profiles loaded` 才失敗 | 不建任何目錄 |

   - 修法：把 `check_backend_env` 移到 `load_profiles` 與「no profiles loaded」之後、Windows passthrough 注入之前，也就是 Python 第一次呼叫 `select_device_backend()` 的位置。計畫 Step 3 原本寫的是舊順序，要一起修訂計畫。補一個參數化測試涵蓋上面三列。

2. **[Medium] `daemon_main.rs:437,446` — 隔離判定沒先 strip，Python 會拒絕的 Rust 會放行。**
   - 問題：`isolation_reason` 拿未 strip 的 env 原值去做 `normalize_path`。Python `_isolation_reason` 是先 `raw.strip()` 再正規化。
   - 實測：`SERIALWRAP_RUN_DIR="$XDG_STATE_HOME/serialwrap/run "`（尾端有空白）加上 allowlist。Python rc=2，`not an isolated instance: SERIALWRAP_RUN_DIR equals default`；Rust 照常啟動並啟用 allowlist。Stage 0 的 kill／pause 鉤子也用同一個判定，所以一樣受影響。
   - 修法：第 446 行改成 `normalize_path(f, py_strip(raw), …)`，並在 `allowlist_rejected_reasons_exact` 加上前後有空白的案例。

3. **[Medium] `daemon_main.rs:592`（`yaml_exc_name`）— 壞 config.yaml 的例外類別名有 4 種跟 Python 不同。**
   - 問題：用訊息字串猜例外類別，只有 golden 裡的 13 個案例對得上。實測不一致的輸入（每列都是 `reason invalid config.yaml: <類別>`）：

     | config.yaml 內容 | Python | Rust |
     |---|---|---|
     | `a: !!timestamp foo` | `AttributeError` | `ValueError` |
     | `a: !!bool maybe` | `KeyError` | `ConstructorError` |
     | `a: &x 1\nb: &x 2\nc: *x` | `ComposerError` | `ScannerError` |
     | `%YAML 2.0\n---\na: 1` | `ParserError` | `ScannerError` |

   - 修法：照 PyYAML 的實際行為補對應（duplicate anchor → `ComposerError`；incompatible YAML document → `ParserError`；未知 bool 值 → `KeyError`；timestamp 無法解析 → `AttributeError`）。同時把這 4 列加進 `scripts/rust/goldens/gen_allowlist_unreadable_config.py` 的 `CASES`，重新產生 golden。

4. **[Medium] `crates/sw-daemon/tests/daemon_process.rs:470-471` — 這個 Review Focus 3 測試在非 root 下一定失敗，commit 回報的通過數不實。**
   - 問題：測試先 `chmod 555` 再刪 `raw.wal.ndjson`，非 root 一定 EACCES。本機實跑結果就是這個測試 FAILED。在 root 環境，這個測試和 `wal_dir_readonly_at_start_blocks_login_probe` 都會直接 return，但結果仍算 passed。commit 寫的「27 passed／1 failed」把這兩個當成通過，可是計畫明訂 root 下 skip 是證據不足，不能算 PASS。
   - 驗證：我在 `/tmp/rv` 把兩行對調（先刪檔再 chmod），三個 `wal_` 測試都在非 root 下通過。所以問題在測試寫法，產品行為沒問題。
   - 修法：
     - 對調第 470、471 行。
     - root 下的 skip 改成會讓測試報告標示「未執行」，例如加 `#[ignore]` 或用 env 開關控制，不能悄悄 return。
     - 在 commit 驗證段更正：Review Focus 3 的兩個真實 daemon 測試要附非 root 實跑結果。

5. **[Medium] `crates/sw-daemon/src/launcher.rs` — 計畫要在這個 Task 切換的 launcher 沒有切換。**
   - 問題：計畫 Files 欄與 `launcher.rs:249` 的註解都寫明 P5-T19 要切換 `implementation == "rust"`，讓它走 `daemon_main::run`。實作沒改，理由是已鎖定的 `launcher_rejects_rust_implementation_in_stage1` 要求 exit 2。這是計畫項目未完成，commit 卻放在「與計畫的差異」，沒列入 BLOCKED-OWNER。後果是 manifest 指向 Rust 時，release bundle 無法起 Rust daemon，Task 20 的 A/B 會被擋住。
   - 修法：改列為 BLOCKED-OWNER，請擁有者裁決。兩個方向擇一：一是 add-only 新增 `launcher_dispatches_rust_implementation`，舊的鎖定測試依狀態推進協定處理；二是修訂計畫，把切換移到後續 Task。

6. **[Low] `scripts/rust/goldens/gen_allowlist_unreadable_config.py` — 新 golden 無法驗證新鮮度。**
   - 問題：產生器把輸出路徑寫死，沒有 `--out`；也沒有加進 `tests/test_rust_goldens_fresh.py` 的 `GENERATORS`。該檔明訂「每個 golden Task 追加一列」。
   - 修法：比照其他產生器加 `--out`，並在 `GENERATORS` 登記（permission 那列在 root 時用 setpriv 的分支照現有寫法保留）。

7. **[Low] `daemon_main.rs:820-821` — 暫停點的寫檔錯誤被吞掉。**
   - 問題：`release_persist_point` 用 `let _ =` 丟掉 mkdirs／寫 `arrived` 的錯誤。Python 的 `_test_release_persist_point` 在這裡出錯會拋例外，release 持久化跟著失敗。
   - 修法：`before_release_commit` 改成回傳 `Result`；失敗時讓這次 commit 回 `Err`，維持 release persist pending。

8. **[Low] 計畫 Step 4 的 `cargo build --release --target x86_64-unknown-linux-gnu` 沒有執行。**
   - commit 已揭露（磁碟配額），但 Task 20 需要這個 release binary。這項不能視為完成，應列入未完成清單。

Windows 部分沒有被假裝完成：`s21_core.windows` 的新 nodeid、`windows_console_ctrl_triggers_orderly_stop`、TCP endpoint 都明確標成 LOCAL-WINDOWS-TODO。

### 7a84168 fix(sw-core): 動態偵測的 PROBE TX 改經 TxWriter／Fence 送出

1. **[Medium] `crates/sw-core/src/session_manager.rs:1144-1145` — PROBE 被拒送時，Rust 仍會判定模板，Python 會直接放棄。**
   - 問題：`probe_send` 不回傳送出結果，被 Fence 拒絕（WAL 不健康、frozen 等）時，`detect_template` 照樣睡 3 s，再拿 RX 快照比對模板。Python 的 `bridge.send_command("")` 在 WAL 不健康時會拋 `WalUnavailableError`，`TxIncompleteError` 也一樣；呼叫端 `except Exception` 接住後 `detected=None`，直接走 fallback，也不睡那 3 s。
   - 具體例子：WAL 不健康時，DUT 在那 3 s 內自己吐出 `root@prpl:/# `。Python 的 `profile_source` 是 `fallback`（passthrough）；Rust 會是 `detected`（prpl 模板），接著進入登入／probe 流程。
   - 修法：`probe_send` 回傳 `TxOutcome`。結果不是 Completed 時，`detect_template` 立刻回 `None`，不睡、不比對。補一個測試：用 `fanout` 設 unhealthy（不是只用 freeze），讓假 DUT 主動送出 prompt，斷言結果是 `fallback`、OS 提交次數為 0、clock 沒有前進 3 s。

### edfc741 test(sw-daemon): daemon_stop_during_force_recover_with_client_gone 改以 Python 實測語意斷言

1. **[Medium] `crates/sw-daemon/tests/daemon_process.rs:497-540` — 改寫後的測試蓋不到原本要防的風險，而且是實作者自行裁決。**
   - 問題 1（流程）：測試的意思從「管線中的 stop 要被處理，15 s 內 exit 0」整個反過來，變成「管線中的 stop 不被處理」，commit 自己寫「待擁有者確認」卻已經讓測試轉綠，等於把 3938032 列的 BLOCKED-OWNER 消掉了。
   - 問題 2（證據）：Python 基線的實測腳本沒有提交，無法追溯。我讀 `sw_core/rpc_posix.py:serve_connection` 的程式，推斷這個說法合理（`drain` 拋例外後跳出迴圈，後面的行不再處理），但沒有重現實測。
   - 問題 3（覆蓋）：Review Focus 5 真正要防的是「停機時 handler 還握著 session，WAL 被提前關掉」。改寫後的版本完全沒有測到真實 daemon「停機和進行中的 recover handler 同時發生」的情境。
   - 修法：
     - 保留現在的 Python 對照斷言，把實測腳本提交到 `tests/contract_blackbox/slice21/`，並把這項裁決寫進 parity-amendments／owner-decisions，等擁有者確認。
     - 另外新增測試：force recover 進行中，從**第二條連線**送 `daemon.stop`（或送 SIGTERM）。斷言 15 s 內 exit 0、救援 TX 已寫入 WAL、WAL 最後一行完整；再加一個 in-process `run_with_hooks` 版本，用 trace 斷言 `WalShutdown` 發生在 recover handler 結束之後。

2. **[Medium] `daemon_process.rs:518-525` — 判斷 recover 已收斂的條件太寬，回歸時測試照樣會過。**
   - 問題：等待條件接受 `READY`，但 COM0 一開始就是 `READY`。如果 recover 還沒把狀態切到 RECOVERING，等待會立刻通過；接著只睡 2 s 就檢查 `try_alive`。萬一 Rust 改成在 handler 結束（約 10 s）後才處理管線中的 stop，這個測試仍會通過，「不得被處理」的斷言就沒有意義。
   - 修法：先等到看見非 READY 狀態（RECOVERING／ATTACHING），再等它回到終態；或者等待時間超過 force recover 的上限（10 s 再加餘裕）。之後再斷言 `try_alive()`。

### High／Medium 摘要表

| # | 等級 | Commit | 位置 | 問題 | 修法 |
|---|---|---|---|---|---|
| 1 | Medium | 3938032 | `daemon_main.rs:907` | 後端 env 檢查早於 argparse／allowlist，exit code 與 Python 不同（2→1） | 移到 `load_profiles` 之後，並修訂計畫 |
| 2 | Medium | 3938032 | `daemon_main.rs:437,446` | 隔離判定未 strip，Python 拒絕的 Rust 放行 | 用 `py_strip(raw)` 再正規化，補測試 |
| 3 | Medium | 3938032 | `daemon_main.rs:592` | 4 種 YAML 錯誤的例外類別名不一致 | 補對應，並加進 golden 產生器的 `CASES` |
| 4 | Medium | 3938032 | `daemon_process.rs:470-471` | 非 root 下測試必敗；root 下 skip 被算成通過 | 對調兩行、root 下明確標示未執行、更正 commit 驗證段 |
| 5 | Medium | 3938032 | `launcher.rs` | 計畫要切換的 rust 分派沒切換，未列 BLOCKED-OWNER | 列 BLOCKED-OWNER，請擁有者裁決或修訂計畫 |
| 6 | Medium | 7a84168 | `session_manager.rs:1144-1145` | PROBE 被拒送後仍判定模板，Python 會走 fallback | 依送出結果提早回 `None`，補 WAL 不健康的測試 |
| 7 | Medium | edfc741 | `daemon_process.rs:497-540` | 自行反轉計畫斷言，實測證據未提交，失去「停機與 handler 並行」的覆蓋 | 送擁有者裁決、提交實測腳本、新增第二連線停機與 trace 順序測試 |
| 8 | Medium | edfc741 | `daemon_process.rs:518-525` | 收斂條件接受初始的 READY，回歸時測試仍會過 | 先看到非 READY 再等終態，或等超過 10 s 上限 |
