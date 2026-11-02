> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

I have initiated the test execution and will wait for it to complete.
I will wait for the test task to complete.
Waiting for cargo test execution to complete.
Waiting for the cargo test task to complete.
I have scheduled a timer to wait for the background test task to finish.
I will wait for the test to complete.
I am waiting for the background test execution to finish.
I am waiting for the background cargo test task to finish.
I will wait for the test to complete.
Waiting for cargo test to finish.
Waiting for cargo test to finish.
Waiting for test task to complete.
Waiting for test suite to finish.
I will wait for the test to complete.
I will wait for the timer.
Waiting for the timer to finish.
Waiting for timer.
Waiting for timer to expire.
Waiting for background task notification.
I am waiting for the timer.
Waiting for the timer to finish.
Waiting for timer.
Waiting for timer trigger.
I will wait for the timer.
I will wait for the timer.
Waiting for task-145 timer.
I will wait for the timer.
## A 前輪 finding 核對

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---|---|
| 1 | Medium | 已修 25ad3900 | 0 | `OpSpec::InteractiveOpenCmd` 的 `lease_send` 帶入 `grant.cmd_id`；未完整送出時依 `map_send_failure` 結算為 `COMMAND_ABORTED_IN_FLIGHT` 並帶 META。紅燈先行回歸 `interactive_mode_command_tx_carries_cmd_id`、`interactive_mode_command_partial_send_maps_aborted_in_flight`、`interactive_mode_command_unknown_send_maps_aborted_in_flight_unknown` 皆通過。 |
| 2 | High | 已修 25ad3900 | 0 | `ManagerExec` 引入有界工作者（`JobQueue`：`open_jobs`、`persist_jobs`），開埠與 release 持久化等待移出 actor 執行緒，以 `BridgeOpened`／`StateCommitted` 事件非同步回報。開埠卡住時 `GetState` 仍可回覆、release 於逾時後回 `DRAIN_STALLED`。紅燈先行回歸 `product_executor_blocked_open_keeps_actor_responsive_and_release_drain_stalled` 通過。 |
| 3 | Medium | 已修 25ad3900 | 0 | 新增 `SessionRequest::SetAlias` 請求，`alias_set` 先更新 manager 鏡像後發送給 actor 同步更新其 profile 並等待確認後才 commit。紅燈先行回歸 `alias_set_reaches_actor_profile_for_get_state` 通過。 |
| 4 | Medium | 已修 25ad3900 | 0 | `Shared::bootstrap_sessions` 尾端 commit 失敗時記錄 Python 例外末行於 `startup_error`，daemon 啟動時檢測並輸出 traceback 後以 exit code 1 退出，停止後續裝置掃描與 attach。紅燈先行回歸 `startup_save_failure_reports_python_exception_line`、`unwritable_state_dir_startup_save_exits_1_before_attach` 通過。 |
| 5 | Medium | 已修 25ad3900 | 0 | `parse_args_width` 遇到獨立 `--` 且無 positional 時保留於 extras，對齊 Python 3.12 argparse 輸出 `unrecognized arguments: -- ...` 並以 exit code 2 退出。紅燈先行回歸 `bare_double_dash_is_unrecognized_exit_2` 通過。 |
| 6 | High | 已修 25ad3900 | 0 | `probe_send` 初始化 PROBE fence 後呼叫 `self.fanout.register(fence.clone())` 註冊全域 fanout，及時接收 WAL unhealthy 通知以拒絕自動 TX。紅燈先行回歸 `detect_probe_fence_observes_wal_unhealthy_published_before_submit` 通過。 |
| 7 | Medium | 已修 25ad3900 | 0 | `ManagerNote::EvictCapture` 內部路由改以 `profile.com == com` 精確比對尋找 owner actor，不再受使用者 alias 規則干擾。紅燈先行回歸 `global_bg_capture_eviction_owner_routing_ignores_alias_collision` 通過。 |
| 8 | Medium | 已修 25ad3900 | 0 | 抽出 `build_expect()` 供 `assemble` 與 `judge` 統一共用，重新載入並驗證 parity 來源、雜湊與逐列狀態，防止放寬結果。紅燈先行回歸 `test_cmd_judge_reloads_parity_like_assemble` 通過。 |

## B 新發現

NO FINDINGS

本輪範圍 `43359a47..d0adf2fe` 僅包含單一提交：
- `d0adf2fe fix: 僅登記明確能力 selector 的 ignored 節點`

依據編排者裁決（P5 第 8 輪 d4c 驗收裁決：擴充 `regen_lock` 僅對能力集合放行），實作透過嚴格正則 `_CAP_IGNORED_FILTER_RE`（`cap_([a-z][a-z0-9]*(?:_[a-z0-9]+)*)__`）與參數規格檢查（`argv.count("--") == 1`、`len(libtest_args) == 2`、`libtest_args[0] == "--ignored"`），確保僅精確匹配 `-- --ignored cap_<cap>__` 之能力 selector 會納入同能力 ignored 節點至 add-only 差集。既有集合與新增集合皆正確處理，非能力集合維持扣除 ignored 且移除邏輯維持 `--allow-removal` 逐名核准。回歸測試（2 支新增測試含 5 組反向篩選參數）與契約檢查（`tools.contracts check`）、policy check（25 pass / 0 fail / 2 warn）、Cargo 測試（`sw-core`、`sw-daemon`、`sw-pal` 全數通過）均確認無引入新缺陷。

## 既有碼待辦（不計入結論）

1. `tests/test_l1_judge.py` 中的 `test_l1_lock_frozen_set_excludes_capability_gated` 與 `test_l1_lock_cargo_sets_exclude_ignored_tests` 斷言 `s21_core:windows` 含有能力 SKIP 案例與 `#[ignore]` 列；此為既有已知待辦項（先前 commit `25ad3900` 已標記為 BLOCKED-OWNER，待擁有者裁決以 `--allow-removal` 移除）。

## 對裁決的異議（不計入結論）

無

結論：可放行
