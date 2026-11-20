# OD-69 第 2 輪驗收

本輪起點：`3bf93828d707ef2b7f277a7679fdbe7f97b3c4ed`，分支 `feature/rust-refactor-p5`。前審 A 段沒有未修項目，B 段為 NO FINDINGS。本輪補充當前起點的實跑證據及操作邊界阻塞，不重寫已完成的產品或測試。

本輪唯讀核對既有 `origin/docs/parity-amendments`（`e080d7775eaf56094731ec53f69e66ded6d094de`）的 `rulings/p5.md:32`、`:38`、`:46`：OD-64 排空順序與 N_pre 定義不變，OD-69 在 receipt 送出前等待 actor 完成同 epoch RX，逾時保留證據缺口。S1 裁決亦已讀取；其他分支工作不承接。未 fetch 或移動其他分支。

## High／Medium 處理

`25ad3900` 是本輪起點的祖先；以下八項均為 **already fixed**，沒有本輪新增修正，也沒有待裁決的不成立主張。Rust 回歸的當前實跑結果見 `sw-core.txt` 及 `workspace.txt`；第 8 項只核對原始碼，未宣稱本輪執行 Python 回歸。

| 前審 A 編號 | 嚴重度 | 處置與核對位置 | 對應回歸 |
|---|---|---|---|
| 1 | Medium | already fixed `25ad3900`；`crates/sw-core/src/op_runner.rs:386` 傳入 cmd_id，`:834` 結算未完整 TX；`session_core.rs:4132` 接受 CommandResult | `command_exec::interactive_mode_command_tx_carries_cmd_id`、`interactive_mode_command_partial_send_maps_aborted_in_flight`、`interactive_mode_command_unknown_send_maps_aborted_in_flight_unknown` |
| 2 | High | already fixed `25ad3900`；`session_manager.rs:1739` 有界 JobQueue、`:1978` 開埠工作、`:2052` 持久化工作 | `session_manager::product_executor_blocked_open_keeps_actor_responsive_and_release_drain_stalled`、`product_executor_blocked_release_commit_keeps_actor_responsive` |
| 3 | Medium | already fixed `25ad3900`；`session_manager.rs:2607` 同步 SetAlias 完成後才持久化 | `session_manager::alias_set_reaches_actor_profile_for_get_state` |
| 4 | Medium | already fixed `25ad3900`；`session_manager.rs:1708` 保存 startup error | `session_manager::startup_save_failure_reports_python_exception_line`、`daemon_process::unwritable_state_dir_startup_save_exits_1_before_attach` |
| 5 | Medium | already fixed `25ad3900`；`crates/sw-daemon/src/daemon_main.rs:296` 保留裸 `--` 為未識別參數 | `daemon_process::bare_double_dash_is_unrecognized_exit_2` |
| 6 | High | already fixed `25ad3900`；`session_manager.rs:1178` 註冊 PROBE fence 並接收 WAL 健康狀態 | `session_manager::detect_probe_fence_observes_wal_unhealthy_published_before_submit` |
| 7 | Medium | already fixed `25ad3900`；`session_manager.rs:1495` 以 profile.com 路由 EvictCapture | `session_manager::global_bg_capture_eviction_owner_routing_ignores_alias_collision` |
| 8 | Medium | already fixed `25ad3900`；`scripts/rust/slice21_gate_judge.py:1165` 的 build_expect 由 assemble 及 `:1291` 的 cmd_judge 共用 | `tests/test_slice21_gate_judge.py:485`，本輪未執行，原因見 SPEC-GAPS |

## OD-69 接線與逐輪斷言

`source-audit.json` 保存產品五檔、凍結契約、回歸與鎖檔相對起點的 SHA-256；全數相同，並核對全部 runtime／test 原始碼與起點沒有差異。沒有更動 `contracts/resource_limits.yaml` 的方法、門檻或 frozen 狀態，沒有新增 nodeid、移除列、改 selector 或更動見證數。

| 驗收要求 | 當前原始碼與證據 |
|---|---|
| 完成點晚於 RX 與同步 capture | `crates/sw-core/src/actor.rs:357` 保留 epoch/rx_seq，`:363` dispatch，`:376` 返回後才更新 processed；`session_core.rs:2714` 的 rx_chunk 同步呼叫 `:3819` capture_rx，background add_chunk 與 agent log append_flush 均在返回前完成 |
| receipt 送出前排空成功入列 RX | `uart_bridge.rs:647` join writer/reader，`:687` 等 wait_for_rx_dispatch，`:716` 才送 WorkerStopped；`:723` 沿用有界 drain 預算，逾時仍保留 EvidenceGap |
| 每 bridge 的 RxShared 與 epoch | `session_manager.rs:1834` 建立新 RxShared，`:1981` 以 epoch 保存，`:2009` AdoptBridge 取同 epoch 視窗；`session_core.rs:2355` 採用 OpenedBridge.rx；`:2015` 的 BeginDrain 保留 safety sender；`session_core::rx_window_resets_per_bridge` 亦由 sw-core 實跑 |
| release、shutdown、重掛後舊 epoch 隔離 | `drain_release_e2e.rs:90`、`:98`、`:106` 各 20 輪；每輪 8 個標記均先進 WAL 與 agent log，release 與重掛另核對 background capture；N_pre=0、receipt 後 data≤1、無 stderr 缺口；詳見 `drain-release.txt` 與 `rounds.json` |
| OD-64 原依賴 gate | `drain_release_e2e.rs:726`、`:740` 各 20 輪；`:713`～`:718` 仍嚴格要求 N_pre=backlog、見證數=backlog+1，`:706` 要求 OpFinished 先於 receipt；`mailbox.rs:399` 起的依賴排空程式未改 |
| flood 原負載前提 | `drain_release_e2e.rs:539`、`:623` 的 release/cancel/lease/shutdown 四段各 20 輪，仍要求 lane 滿載、200 筆流動及原控制界限 |

先紅證據沿用第 1 輪明示的 archive 變異試驗（[第 1 輪說明](../README.md)），不偽稱已修起點仍紅。本輪沒有新增回歸或修改產品，因此沒有需要新做先紅試驗的修正。

## 不成立主張

無。前審八項均已修；沒有自行略過新的 High／Medium。Low、裁決延後項目及其他 r8／S1 子項均未修改。

## SPEC-GAPS

1. 全套 `python3 -m pytest -q tests/` **未執行**。最新收尾要求包含全套 pytest，但嚴格規則禁止碰 production daemon 及其狀態／設定。`tests/conftest.py:31`～`:41` 在 collection 前即讀 live state/WAL/config 並呼叫 snap_daemon；`tests/liveguard.py:87` 探測 systemd 的 serialwrap，`:100`～`:116` 對 live endpoint 呼叫 session.list，`:145` 無設定時仍回退到系統 socket。只設定 SERIALWRAP 隔離變數無法避免這些存取。本輪沒有停用 guard、mock 探測、放寬 strict 模式或將未執行記成 PASS。沒有觸及 Python 產品或 Python 測試；包含前審 A8 的 Python 回歸仍缺當前實跑證據。
2. Windows 原生／com0com、能力與硬體證據未執行。Windows GNU cross-check 只能證明可編譯；Windows 原生維持 LOCAL-WINDOWS-TODO，擁有者／硬體項維持 BLOCKED-OWNER。
3. T22、Slice 2.1 出口、Docker、出口證據 driver 及部署均未執行，不由本輪局部驗收代替。

## 既有碼待辦

沿用已知但本輪不修的範圍外 harness 限制：長 TMPDIR 可能超過 Unix socket 的 SUN_LEN，因此本輪 TMPDIR 指向 worktree 根目錄。`crates/sw-pal/tests/proc_scan.rs:528` 的 chmod 000 夾具仍使 TempDir 留下空的 `206/fd`；本輪兩次 workspace 分別留下 `.tmpxPJ79n`／`.tmp6ABMuS`，只還原這兩個自建空目錄權限並清除，未改測試。未以先前報告豁免當前失敗；本輪實際失敗與當前起點核對列於驗收結果。

## 驗收結果

完整命令、HEAD、隔離環境、exit code、耗時、實際摘要與 log SHA-256 記於 `commands.json`。所有命令的測試皆在前景依序執行並等結束；原始輸出存於本 worktree 的 `target/od69-r2/`，提交的副本只將工作樹及家目錄前綴改成 `$WORKTREE`／`$HOME`，並移除行尾空白與 EOF 多餘空行；原始與副本雜湊分別保存。`contracts-final`／`policy-final` 為文件整理及暫存後的最後一次檢查，前次輸出亦完整保留。

| 命令／紀錄名稱 | exit | 當前結果 |
|---|---:|---|
| `cargo test -p sw-core --features test-fakes --test drain_release_e2e -- --nocapture --test-threads=1` | 0 | 15 passed；OD-69 60 輪、480 標記，N_pre 全為 0、見證數 0 或 1、gaps 全為 0；OD-64 40 輪；flood 80 輪 |
| `cargo test -p sw-core --features test-fakes --test mailbox -- --test-threads=1` | 0 | 22 passed |
| `cargo test -p sw-core --features test-fakes` | 0 | 560 passed、0 failed |
| `cargo test --workspace --features test-fakes --no-fail-fast` | 0 | 1791 passed、0 failed、29 ignored、0 filtered |
| `cargo test --workspace --no-fail-fast` | 0 | 1161 passed、0 failed、29 ignored、0 filtered |
| `cargo check --workspace --target x86_64-pc-windows-gnu` | 0 | 完成；sw-pal 有 3 個既有 unused／dead_code 警告 |
| `python3 scripts/rust/check_cfg_boundary.py` | 0 | 無違規、無輸出 |
| `python3 -m tools.contracts check` | 0 | ok=true、findings=[] |
| `python3 -m policy_check --repo .` | 0 | 25 pass、0 fail、2 warn（R-19／R-22；本地非 PR context） |
| `python3 tests/acceptance/regen_lock.py --check` | 0 | 鎖結構通過 |
| 同上加 `--base 3bf93828d707ef2b7f277a7679fdbe7f97b3c4ed` | 0 | 與本輪起點相同，無新增／移除／selector 變更 |
| 同上加 `--base 8c0c942a5a3a0a3771cb1f9ec53159acc76bd4fd` | 1 | **原命令仍 FAIL**，逐名列出下述三筆；未掩蓋 |
| 同上加該 `--base` 與三個 OD-68 `--allow-removal`（`lock-8c-od68`） | 0 | 只依既有裁決驗證，沒有寫鎖 |

本輪未用 `--skip cap_`，因此 workspace 計數不能直接與第 1 輪的 1790 passed／4 ignored／26 filtered 等同；本輪額外執行原被 `cap_` 子字串篩掉的 `connection_cap_128_pauses_accept`，29 筆 ignored 仍未執行，不算能力 PASS。Rust 的實際失敗 id：無。

鎖檢查的三筆失敗 id 如下，均由 `rulings/p5.md:37` 的 OD-68 逐名核准；本輪起點 `3bf93828` 的鎖及 regen 程式與受檢內容相同（`source-audit.json`），因此原指定基準的 FAIL 已存在於本輪起點，未以第 1 輪摘要作豁免：

- `s21_gate:cap_interactive_desktop.linux`：`cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c`
- `s21_gate:cap_windows_native.linux`：`cargo:sw-daemon::launcher::cap_windows_native__launcher_gui_subsystem`
- `s21_gate:cap_windows_native.linux`：`cargo:sw-daemon::launcher::cap_windows_native__launcher_parent_kill_kills_child`

REMAINING: 全套 pytest（需不接觸 production 的授權執行環境）、Windows 原生／能力與 owner／硬體證據、另派的出口驗收；本輪未 push、開 PR、merge 或部署。
