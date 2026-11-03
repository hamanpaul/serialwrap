> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `25ad3900` | 0 | [`op_runner.rs`](file://crates/sw-core/src/op_runner.rs#L301-L306)：`OpSpec::InteractiveOpenCmd` 的 `lease_send` 已傳遞 `grant.cmd_id`，未完整送出依 `map_send_failure` 結算（`COMMAND_ABORTED_IN_FLIGHT` 且 META 帶 abort 資訊），`PendingOp::InteractiveCmd` 亦接受 `CommandResult`；由 [`command_exec.rs`](file://crates/sw-core/tests/command_exec.rs) 回歸鎖定。 |
| 2 | High | 已修 `25ad3900` | 0 | [`session_manager.rs`](file://crates/sw-core/src/session_manager.rs#L1726-L1765)：`ManagerExec` 新增 `open_jobs` 與 `persist_jobs` 有界工作者（`JobQueue`），開埠與 commit 等待移出 actor 執行緒，完成以 `BridgeOpened`／`StateCommitted` 回報；由 [`session_manager.rs`](file://crates/sw-core/tests/session_manager.rs) 的 ManagerRig 測試鎖定。 |
| 3 | Medium | 已修 `25ad3900` | 0 | [`session_manager.rs`](file://crates/sw-core/src/session_manager.rs#L2607)：新增 `SessionRequest::SetAlias`，`alias_set` 更新鏡像後送至 actor 更新其 profile 並等待確認，使 `GetState` 回應與鏡像一致；由 `alias_set_reaches_actor_profile_for_get_state` 鎖定。 |
| 4 | Medium | 已修 `25ad3900` | 0 | [`session_manager.rs`](file://crates/sw-core/src/session_manager.rs#L1709-L1711)：建構子存檔失敗時將 Python 例外末行記錄至 `startup_error()`，daemon 於裝置掃描前以 rc 1 結束；由 `startup_save_failure_reports_python_exception_line` 及 [`daemon_process.rs`](file://crates/sw-daemon/tests/daemon_process.rs) 鎖定。 |
| 5 | Medium | 已修 `25ad3900` | 0 | [`daemon_main.rs`](file://crates/sw-daemon/src/daemon_main.rs#L296-L301)：`parse_args_width` 於無 positional 時遇到獨立 `--` 將其保留至 extras，觸發 `unrecognized arguments: --` 並以 rc 2 結束；由 `bare_double_dash_is_unrecognized_exit_2` 鎖定。 |
| 6 | High | 已修 `25ad3900` | 0 | [`session_manager.rs`](file://crates/sw-core/src/session_manager.rs#L1178)：PROBE fence 建立後呼叫 `self.fanout.register(fence.clone())` 向全域 fanout 註冊，確保快照後發布的 unhealthy 能及時阻擋自動 TX；由 `detect_probe_fence_observes_wal_unhealthy_published_before_submit` 鎖定。 |
| 7 | Medium | 已修 `25ad3900` | 0 | [`session_manager.rs`](file://crates/sw-core/src/session_manager.rs#L1495-L1505)：`ManagerNote::EvictCapture` 改以 `profile.com == com` 精確比對尋找 owner session，不使用 alias-aware `resolve()`；由 `global_bg_capture_eviction_owner_routing_ignores_alias_collision` 鎖定。 |
| 8 | Medium | 已修 `25ad3900` | 0 | [`slice21_gate_judge.py`](file://scripts/rust/slice21_gate_judge.py#L1165)：抽出 `build_expect()` 供 `assemble` 與 `judge` 入口共用，重新載入並驗證 parity 狀態檔、來源與逐列狀態；由 `test_cmd_judge_reloads_parity_like_assemble` 鎖定。 |

---

## B 新發現

### P5-a68d042-1
[Medium] [crates/sw-pal/src/process_tunnel/posix.rs:17](file://crates/sw-pal/src/process_tunnel/posix.rs#L17) 與 [crates/sw-pal/tests/serial_posix.rs:514](file://crates/sw-pal/tests/serial_posix.rs#L514) — `process_tunnel` 未接上 `test-fakes` 的 pre-exec 暫停鉤子，導致 `cap_posix_pty__native_attach_spawn_process_tunnel` 恆常以「暫停接線缺漏紅燈」panic 失敗；而 `a68d042d` 卻將該未通過的紅燈測試登記至 `tests/acceptance/l1_required.lock.json` 的 `s21_gate:cap_posix_pty` 必要集合中，造成 gate `linux_cap_posix_pty` 失敗。

- **具體失敗情境**：
  - **輸入**：以 `--features test-fakes` 執行 `cargo test -p sw-pal --test serial_posix -- cap_posix_pty__native_attach_spawn_process_tunnel --ignored`，或以 gate 收集器執行 `python3 scripts/rust/cargo_l1_results.py --platform linux --caps posix_pty --caps-only --out /tmp/cap-posix-pty-results.json`。
  - **預期輸出**：`process_tunnel` 之子行程在 pre-exec 觸發暫停鉤子並向測試夾具回報 PID，在 attach 開埠驗證自身子行程不被誤判為外部 holder（`self_process_tree` 排除生效）後順利放行結束，測試結果為 `test ... ok`，exit code 0。
  - **實際輸出**：[`NativeProcessTunnel::spawn_detached()`](file://crates/sw-pal/src/process_tunnel/posix.rs#L17) 未串接 [`take_sysenv_spawn_pause()`](file://crates/sw-pal/src/conformance/mod.rs#L127) 暫停鉤子，導致測試端未收到 pre-exec PID 通知而拋出 `Custom { kind: UnexpectedEof, error: "sysenv spawn 在 pre-exec 通知前結束" }`，進而被測試夾具 `catch_unwind` 捕捉後觸發 panic：「`未取得 process_tunnel pre-exec 通知；暫停接線缺漏紅燈（未收到子行程進入 pre-exec 的 PID 通知: Custom { kind: UnexpectedEof, error: "sysenv spawn 在 pre-exec 通知前結束" }）；清理結果：None`」，測試結果為 `test ... FAILED`（exit code 101）。由於 commit `a68d042d` 已將此 nodeid 登記至 [`l1_required.lock.json`](file://tests/acceptance/l1_required.lock.json#L11170) 的 `s21_gate:cap_posix_pty` 必要集合中，直接造成 gate `linux_cap_posix_pty` 失敗。
- **確切修法**：
  1. 在 [`crates/sw-pal/src/process_tunnel/posix.rs`](file://crates/sw-pal/src/process_tunnel/posix.rs#L17) 的 `spawn_detached()` 中，比照 [`crates/sw-pal/src/sysenv/posix.rs:238`](file://crates/sw-pal/src/sysenv/posix.rs#L238) 於 `#[cfg(all(unix, feature = "test-fakes"))]` 時檢查 `crate::conformance::take_sysenv_spawn_pause()`；在 `command.pre_exec()` 內執行暫停交握（回報 child PID 至 `notify_write_fd`、poll `release_read_fd` 並讀取 release byte），再執行 `setsid()` 與 `close_extra_fds()`（或抽取共用交握邏輯）。
  2. 修復後實跑驗證 `cargo test -p sw-pal --test serial_posix --features test-fakes -- cap_posix_pty__native_attach_spawn_process_tunnel --ignored` 確為 `ok`，使登記入 `l1_required.lock.json` 的 nodeid 轉綠。

---

## 既有碼待辦（不計入結論）

無

---

## 對裁決的異議（不計入結論）

無

---

結論：需修正
