# P6（Slice 2.2）接線＋端到端驗收

> 依 [`review-convergence-rules.md`](https://github.com/hamanpaul/serialwrap/blob/docs/parity-amendments/docs/rust-refactor/reviews/review-convergence-rules.md) §5（擁有者 2026-10-28 核可）：列出本包新增的 pub fn／欄位，逐一找到正式路徑的呼叫端，並有 daemon 級端到端測試；把任一段改成 no-op 時測試必須會紅。
> 量測點：`feature/rust-refactor-p6` 第四輪（2026-10-29 cloud session），基準為 `origin/feature/rust-refactor-p5`（cca5ba8，已併入本分支），故 `git diff origin/feature/rust-refactor-p5..HEAD -- 'crates/*/src/**'` 即本包（P6）新增的產品碼。

## 1. 新增 pub 介面與正式路徑呼叫端

方法：掃描上述 diff 中新增的 `pub fn`／`pub(crate) fn` 與 `pub` 結構欄位；對每一項在 `crates/*/src/**` 的產品碼（排除 `fakes/`、`testutil.rs`、`conformance/`、`tests/` 與 `#[cfg(test)] mod`）找非定義處的參照。

| 範圍 | 新增項目 | 有正式路徑呼叫端 | 無正式路徑呼叫端 |
|---|---|---|---|
| sw-core | 259 | 247 | 12 |
| sw-pal | 81 | 41 | 40 |
| sw-proto | 3 | 2 | 1 |
| sw-daemon | 1 | 1 | 0 |
| 合計 | 344（fn 212、欄位 132） | 291 | 53 |

無正式路徑呼叫端的 53 項逐一分類：

| 類別 | 項目 | 判定 |
|---|---|---|
| 測試替身／工具（`sw-pal/src/fakes/**`、`testutil.rs`、`conformance/console_suite.rs`，皆 `test-fakes`） | `FakeConsoleFactory` 的 `accept_n`、`failing_read`、`set_client_id`、`written_len`、`push_input`、`hangup`、`connect_with`、`pty_pair_from`、`fail_next_com0com`、`com0com_opened`、`fail_next_mux_waits`、`set_held`；`FakeDevice` 的 `tx_len`、`tx_since`、`set_loopback`、`wait_blocked`、`on_tx_match`；`testutil` 的 termios 欄位（`iflag`／`oflag`／`cflag`／`lflag`／`vmin`／`vtime`）、`tcgetattr_path`、`master_is_nonblocking`、`no_symlink_points_to`、`d01_scan_dirs`、`symlink_snapshot`、`new_symlinks_to`、`is_dev_pts_path`、`wait_accept`、`nodelay`、`reuseaddr`、`tcp_connect_closed_port_error`、`set_comm_state`／`escape_comm_function` 欄位、`comm_api_calls`、`toggle_peer_modem_lines`；`console_suite` | 測試專用，不屬正式介面 |
| `#[cfg(feature = "test-fakes")]` 的產品內測試鉤子 | `CommandRegistry::records_for_test`、`SessionCore::human_console_mut`、`seed_deferred_for_test`、`console_tcp::posix::get_int_opt` | 測試專用 |
| 唯讀觀測 accessor（不改狀態） | `HumanTxLane::in_flight_job`、`backlog_is_empty`、`LineEditor::buffered_len`、`SessionCore::active_lease`、`stashed_human_lease`、`SessionManager::pending_pty_count` | 不承載行為；正式路徑使用其底層欄位 |
| 正式路徑使用同義入口 | `hub::spawn_hub`（正式路徑呼叫 `spawn_hub_with`，`session_manager.rs`）、`SessionCore::invalidate_human_input`（正式路徑呼叫同一本體 `invalidate_input`） | 已接線 |
| 掃描誤判 | `GlobalConfig::windows_serial_console`（`daemon_main.rs` 的 `cfg.windows_serial_console()` 位於檔內 `#[cfg(test)] mod` 之後，被粗略截斷） | 已接線（`daemon_main.rs:1356`） |
| **未接線（本輪修正）** | `SessionCore::set_worker_health`（R3-8 deferred） | 正式路徑原本恆視為健康。本輪改由 manager 已注入的 `SelfTestProbes::bridge_snapshot`（現行 epoch 的 IoWorker 快照）判定 ATTACHED 救援的 worker 健康；`set_worker_health` 降為測試覆寫。紅燈 d2a6e67、修正 30475b8 |

## 2. daemon 級端到端與 no-op 變異

每一列：把「接線點」改成 no-op（其餘不動），執行「偵測測試」，期望 FAIL；結束後還原原檔。daemon 級測試為 `crates/sw-daemon/tests/rpc_console.rs`（in-process `ServiceRig`：真 `SessionManager`＋`Service`＋ConsoleHub＋human TX pump，替身只在 UART／console 傳輸層）。

| # | 接線點（檔案） | no-op 變異 | 偵測測試 | 結果 |
|---|---|---|---|---|
| M1 | 開埠工作者預建哨兵 PTY（`session_manager.rs` `OpenCtx::make_sentinel`） | `sentinel = None` | `rpc_console::bridge_creates_internal_primary_sentinel` | RED |
| M2 | 開埠工作者綁 TCP console listener（`OpenCtx::bind_console_listener`） | `console_endpoint = None` | `rpc_console::attach_tcp_shape` | RED |
| M3 | `Effect::HumanTx` → human TX pump（`ManagerExec::human_tx`） | 丟棄效果 | `rpc_console::console_input_reaches_uart_and_rx_fans_out` | RED |
| M4 | reader 的 hub tap（`IoWorkerArgs.hub`） | `hub: None` | `rpc_console::console_input_reaches_uart_and_rx_fans_out` | RED |
| M5 | 全域 `LeaseIndex`（`Effect::LeaseIndex` Insert） | 不寫索引 | `rpc_console::attach_then_status_has_no_false_not_found` | RED |
| M6 | `Effect::ConsoleOut` → hub（`hub.out`） | 不轉交 | `rpc_console::second_console_local_echo_reaches_its_console`（本輪新增） | RED |
| M7 | `BeginDrain` 停 TCP listener（`HubCmd::StopListener`） | 不送命令 | `rpc_console::tcp_release_stops_listener_accepting`（本輪新增） | RED |
| M8 | `console_attach` 前的 RPC ConsoleScan（`SessionManager::console_attach`） | 移除掃描 | `rpc_console::console_attach_rescans_peers_before_refresh`（本輪新增） | RED |
| M9 | ATTACHED 救援 worker 健康取 `bridge_snapshot`（`lease_p6.rs`） | 分支停用 | `session_core_lease::attached_rescue_uses_manager_bridge_snapshot_for_worker_health` | RED |
| M10 | self-test 各自 resume（`console.rs` `self_test_suspend_end`） | 永不 resume | `rpc_console::overlapping_self_tests_replay_human_input_end_to_end`（本輪新增） | RED |
| M11 | 取消的停放命令結算 registry（`session_core.rs` `settle_parked_terminal`） | 不結算 | `session_core_lease::canceled_parked_command_releases_pending_slot`、`…_then_drain_releases_pending_slot` | RED |

| M12 | adopt 失敗以 detach 語意撤回（`session_manager.rs` `Effect::ConsoleAdopt` 失敗分支送 `SessionRequest::ConsoleDetach`；第四輪續） | 改回只送 `DataEvent::ConsoleDisconnected`（修正前行為） | `rpc_console::attach_request_timeout_closes_pty`（第四輪續新增） | RED（9979795 紅燈：幽靈 lease 留下） |
| M13 | RPC handler 逾時後移除暫存 PTY（`SessionManager::console_attach`） | 不移除 | `rpc_console::attach_request_timeout_closes_pty` | RED（「逾時後暫存 PTY 已移除」） |

另以「還原為修正前原始碼（0123679）」驗證 M10 的偵測測試：`overlapping_self_tests_replay_human_input_end_to_end` 於修正前 FAIL（「兩個 probe 完成後回放 k」等待逾時），修正後 PASS。

M5 首次以 `send_routes_by_lease_index` 偵測時為 GREEN：agent lease 另由 `ManagerNote::InteractiveOpened` 與 `route_session` 的 open 回應寫入索引，只有 human console lease 只靠 `Effect::LeaseIndex`，故改以 human lease 的 `attach_then_status_has_no_false_not_found` 偵測。M6／M7／M8 首次無偵測測試，本輪補上三支 daemon 級測試後皆 RED。

## 3. 本包仍未有 daemon 級端到端者（記錄）

- Windows 原生 console（WSAPoll mux、com0com Serial console、W-1～W-7）：LOCAL-WINDOWS-TODO，只能在擁有者 Windows host 驗證。
- R3-7（`HumanTxCompleted` 回報 `try_send` 失敗的一格重試槽）、R3-4／R5-L6（hub 佇列滿使 AdoptPty 失敗）：需 fault-injection hook，仍 deferred。AdoptPty 失敗的另一來源「PTY 已隨 RPC 逾時丟棄」於第四輪續以 0 逾時取得（M12／M13），與 hub 佇列滿共用同一個撤回分支。
