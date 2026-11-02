> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

正在執行相關測試並等待結果。
等待測試執行完成。
等待契約檢查完成。
等待 policy check 完成。
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 `25ad3900` | 0 | `crates/sw-core/src/op_runner.rs`：`OpSpec::InteractiveOpenCmd` 在 `lease_send()` 帶入 `grant.cmd_id.as_deref()`；未完整送出依 `map_send_failure()` 結算為 `COMMAND_ABORTED_IN_FLIGHT` 並附 META。已補 `interactive_mode_command_tx_carries_cmd_id` 等 3 支測試。 |
| 2 | High | 已修 `25ad3900` | 0 | `crates/sw-core/src/session_manager.rs`：`ManagerExec` 新增 actor 外有界工作者 `JobQueue`（`open_jobs`、`persist_jobs`），開埠與持久化移出 actor 執行，完成後以 `BridgeOpened`／`StateCommitted` 事件回報，解決 actor 卡死及排空逾時失效問題。已補 `product_executor_blocked_open_keeps_actor_responsive_and_release_drain_stalled` 等測試。 |
| 3 | Medium | 已修 `25ad3900` | 0 | `crates/sw-core/src/session_manager.rs`：新增 `SessionRequest::SetAlias`，更新 actor 內部 profile 並等待確認後才 commit，使 `session.get_state` 與 `session.list` 回應一致。已補 `alias_set_reaches_actor_profile_for_get_state` 測試。 |
| 4 | Medium | 已修 `25ad3900` | 0 | `crates/sw-core/src/session_manager.rs`：建構子尾端 commit 失敗記錄於 `startup_error`，daemon 主流程在裝置掃描／attach 前檢查並以 Python 風格 traceback 及 rc 1 結束。已補 `startup_save_failure_reports_python_exception_line` 等測試。 |
| 5 | Medium | 已修 `25ad3900` | 0 | `crates/sw-daemon/src/daemon_main.rs`：`parse_args_width` 遇到獨立 `--` 時保留於 `extras`，對齊 Python 3.12 argparse 無 positional 參數時行為，回報 `unrecognized arguments: --` 並以 rc 2 結束。已補 `bare_double_dash_is_unrecognized_exit_2` 測試。 |
| 6 | High | 已修 `25ad3900` | 0 | `crates/sw-core/src/session_manager.rs`：PROBE fence 初始化後呼叫 `self.fanout.register(fence.clone())` 向全域 fanout 註冊健康通知，確保 WAL 失效時即時拒絕自動 TX。已補 `detect_probe_fence_observes_wal_unhealthy_published_before_submit` 測試。 |
| 7 | Medium | 已修 `25ad3900` | 0 | `crates/sw-core/src/session_manager.rs`：`ManagerNote::EvictCapture` 改以 `profile.com == com` 精確比對 owner，避免因 alias 與他人 COM 同名誤導向錯誤 session。已補 `global_bg_capture_eviction_owner_routing_ignores_alias_collision` 測試。 |
| 8 | Medium | 已修 `25ad3900` | 0 | `scripts/rust/slice21_gate_judge.py`：抽取 `build_expect()` 供 `cmd_assemble` 與 `cmd_judge` 共用，重新載入 parity 狀態與來源檔；來源缺失時維持 INCOMPLETE，不再放寬判定。已補 `test_cmd_judge_reloads_parity_like_assemble` 測試。 |

## B 新發現

NO FINDINGS

## 既有碼待辦（不計入結論）

無

## 對裁決的異議（不計入結論）

無

結論：可放行
