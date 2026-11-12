# S2-PAR-wp510r 第 20 項：關閉生命週期的根因與修正

起點：f661291be0620cecabf7b1541c085a437e269ea6，feature/rust-refactor-p8。
下列根因與盤點在修改 production code 前寫入 `target/s2-par/wp510r/i20/root-cause-plan.md`；表內行號以起點為準，實作後以函式名定位。

依據：三份 fable-wp510r.r1/r2/r3 review.md、定稿設計 v5（含 v1～v4 被引用段）、C1～C8、§3.1.4、§3.1.5、WP4-2，以及 origin/main Python 基線。

## 根因與規劃

三輪全部 findings 共 10 項：

| 類別 | findings | 共同缺口 |
|---|---|---|
| 共用入口未接上帳本 | r1-M1 Plain OpenBridge；r2-M2 三次 panic 的 ClaimForStop；r2-M3 一般開埠期間 bind | 只沿單一窗口修正，沒有將所有入口與同一份所有權協定逐一對照 |
| 所有權／責任生命週期不完整 | r1-M2 Reaper 紀錄未納 shutdown；r1-M3 W6 改名的 owner；r2-H1 停止丟棄已入列 worker | 通知、公開 sid、worker 存活與實際關閉責任混用，轉移後的終態與停止邊界沒被一起驗證 |
| 執行緒／鎖邊界錯置 | r3-M1 建 actor 失敗持鎖 close；r3-M2 回收者同步 close | execute_and_settle 把資源操作與帳本修改綁在同一函式，呼叫端看似完成回收，實際把阻塞 I/O 帶進共用執行緒／鎖 |
| 時間與證據未形成統一出口 | r1-M4 release 期限起點；r3-M3 close 失敗無告警 | 測試只驗最終狀態；沒有同時驗阻塞交錯、實際耗時、配對終態、告警次數與原錯誤 |

前輪修一處又出現另一處：測試按窗口與 review finding 補齊，但 manager、actor executor、回收者各有能自行關閉／結算的捷徑。單一路徑的最終狀態通過，不能證明共用鎖可用、其他 COM 回條可收、停止後責任仍存續。

### 全部 UART close／finish_async／帳本終態入口盤點（起點行號）

| 檔案與函式／位置 | 實際執行緒 | manager 鎖 | 設計責任與處理 |
|---|---|---|---|
| session_manager HandleParts::close:174；AttachOp::close_parts:512、close_preopened_with_receipt:508 | 資源持有者 | 呼叫端決定 | PAL primitive；只供工作者使用，結算另做 |
| close_session_preopened:1035 | 熱插、bind、parked closer | close 時無鎖 | 合併到共用回條結算，保留同步熱插在鎖外收尾與補排 attach 的順序 |
| close_unoffered_custody:1197 | attach／parked offer 呼叫者 | claim 持鎖、close 無鎖 | claim 後交獨立 closer，不阻塞 caller |
| finish_custody_close:1224 | open worker／closer | 結算取鎖 | 統一成功／失敗終態、保留錯誤、watcher 通知與一次告警 |
| spawn_custody_closer:1367；spawn_release_claim_closer:1603 | closer；spawn 失敗退回 caller | 正常無鎖；fallback 由 caller 決定 | 禁止失敗時回退共用執行緒同步關閉；保留所有權並 fail closed |
| close_session_custody_for:1625 | 熱插／shutdown | claim 持鎖、close 無鎖 | claim 後交 closer，使 shutdown 不超出期限 |
| close_before_bridge:1661 | per-session open worker | close 無鎖、結算取鎖 | 保留 worker 關閉，改經統一終態出口 |
| create_session_locked:2006 execute_and_settle；:2020 displaced close | attach／bootstrap | **持鎖** | 鎖內只產生／轉換 job，關閉移交；不可同步 close 或 finish_async |
| spawn_attach_with:2267/2277/2293/2304/2311/2336/2356/2372/2379 | 熱插／manager caller | 大多先解鎖；**2356 持鎖** | 重複／被拒的預開 handle 全部交 closer，保留錯誤告警與阻擋 |
| attach worker cleanup:2453/2486/2545/2553；attach_dynamic:2964/2982/2997/3035；abort_external_attach:3444 | attach worker | close 無鎖 | 已是資源持有工作者，保留，失敗終態透過共用 ledger API |
| open_bridge_custody:4274/4289；open_placeholder_raw:4418；open_bridge_with:4540/4584 | per-session open worker | close 無鎖 | 保留，經 finish_custody_close；configure 等拒絕經 close_before_bridge |
| submit_open_job:4899；submit_placeholder_open_job:4961 | open worker | 無鎖 | worker finish_async 本身只啟動 finisher；Raw 未送達改交 closer |
| retire_placeholder:5174 | actor executor | close 無鎖 | 改交 closer，不阻塞 actor 停止等待 |
| ManagerExec BeginDrain:5425/5430 | actor executor | 自己 worker finish 無鎖；**jobs.execute_and_settle 持鎖** | 自己 worker 啟動 finisher；帳本 jobs 鎖內取出、鎖外交回收者／closer |
| Shared ReaperCallback::handle_cmd:5610/5640/5653 | **唯一回收者** | Held close 無鎖、結算取鎖 | Held 交獨立 closer；worker finish_async 僅啟動專屬 finisher；回收者繼續收其他 COM barrier／回條 |
| WeakSharedReaper:5735；custody send_reclaim_or_finish:482 | 回收者／守衛 drop | 無鎖 | finish_async 為非阻塞啟動；Shared 不在時只能收尾，不能宣告帳本成功 |
| shutdown_phase1:7547/7548/7557 | shutdown RPC | close 無鎖 | parked 交 closer，保持 inflight／帳本直到配對回條，不能直接阻塞 shutdown |
| uart_bridge IoWorker::finish_async:845 → finisher close:876 | 專屬 finisher | 無 manager 鎖 | 正確執行位置，保留 |
| custody ActorExitedJob::execute_and_settle:269 | 由 caller 決定 | 目前兩個 production caller 持鎖 | production 移除此同步捷徑；既有凍結單元測試維持原步驟／斷言，僅 test-fakes 提供 |
| custody settle_close:856、settle_worker_stopped:877、settle_panic:953、settle_claimed_close:714、settle_taken_close:727 | ledger 呼叫者 | production 於 manager 鎖內 | 狀態修改集中 settle_close；worker/panic 先驗身分／建立終態後呼叫它；告警由 Shared 結算出口一次輸出 |
| Shared 無 handle 結算:1062/1253/1455/1568/2803/2830/3456/4149/4335；release watch:2724；finish_worker_op:2759 | actor／worker／manager | 結算取鎖 | NoHandle/Closed 保持；失敗保留真實錯誤，不以 generic close unconfirmed 覆蓋，不重複告警 |

統一規則：manager 鎖內只認領與轉換所有權。manager、actor 與回收者不得同步 close，也不得把持鎖的 ledger 傳入可執行 I/O 的函式；實際關閉由獨立 closer、原已持有資源的 open/attach worker 或 IoWorker finisher 執行。回收者只接命令／回條，finish_async 僅作非阻塞的工作啟動。帳本終態經同一 ledger 函式，Shared 結算出口在鎖外輸出一次 by-id＋原錯誤的告警。CloseFailed 永久阻擋；證據不完整與 handle 關閉失敗分別保留。

### 先紅後綠與驗收規劃

1. HEAD 新增回歸：actor 建構失敗＋close pause 時，query <1 s、另一 session release 在期限內、shutdown 在 budget 內且報未排空；放行 close 後一次關閉、配對 terminal。
2. Held 關閉暫停時，另一個實際 IoWorker 的 RX barrier 與 WorkerStopped 在暫停期間結算；shutdown 對兩個 by-id 的報告正確。
3. Held 與未接手 IoWorker closed:false：各一筆 by-id＋原錯誤告警；Failed 終態；重送回條不重複告警。
4. 既有 DR-9 加實際 <1 s 斷言；新增暫停關閉的建構失敗交錯。補同根因的 BeginDrain／shutdown parked 有界回應測試。
5. 保存 HEAD 紅燈輸出後才改 production；凍結節點不降斷言。新節點 regen_lock --l1-add-only。
6. 受影響 crate（含 test-fakes）、workspace test、build all-targets、clippy、Windows GNU all-targets check、cfg boundary、contracts、lock check、policy、觸及 Python／fragment 測試。全部前景；不跑全套 pytest、slice gate、Docker、出口 driver；不觸 production daemon／真實 UART。
7. ServiceRig RPC 端到端證據；寫實作後對照與完整 gate 數字，合理 commit，乾淨工作樹，不 push。


## 實作與各項 finding 的處理

| finding | 處理 |
|---|---|
| r1-M1 Plain 開埠 gate、r1-M2 Reaper shutdown、r1-M3 W6 改名、r1-M4 RPC 首次期限 | already fixed：398b85f5、21482ece、e35edc09 等起點已有前項修正，本輪未重做；完整 core／daemon 回歸通過 |
| r2-H1 停止時丟棄回收命令、r2-M2 panic 的效果回饋、r2-M3 Plain 開埠 bind 等待 | already fixed：6536d16d、9ab75ab0、f661291b 等起點已有前項修正；本輪保留命令排空、身分配對與等待斷言 |
| r3-M1 建構失敗持鎖關閉 | 本報告同一 commit 修正：`create_session_locked` 在鎖內認領 job，送無界 `RunJob` 命令；回收者跨過原鎖區段後交獨立 closer。attach 工作者以釋放 manager 鎖的 Condvar 等待配對終態，才完成自己的作業 |
| r3-M2 Held 關閉佔住回收者 | 本報告同一 commit 修正：`handle_cmd` 將 Held 交 `spawn_custody_closer`；回收者繼續收其他 COM 的 DrainDataBarrier／WorkerStopped，`finish_async` 只啟動專屬 finisher |
| r3-M3 Held／未接手 worker 的關閉失敗無告警 | 本報告同一 commit 修正：`report_custody_terminal` 根據 Failed 終態告警，與 evidence 是否 Complete 分開。原錯誤保留在 terminal 與 CloseFailed，重複回條不重複告警 |

同根因修正逐一如下：

1. `ManagerExec::BeginDrain` 不再持鎖呼叫會執行 I/O 的 `execute_and_settle`；該同步輔助 API 僅留給 test-fakes 的既有單元節點，production 只能分派 job。
2. `create_session_locked` 的 displaced handle，以及 `spawn_attach_with` 被拒／重複的預開 handle，交同一關閉工作者；移除原本持鎖或忽略關閉錯誤的捷徑。
3. `retire_placeholder` 落盤失敗後必須關閉的資源、Raw 通知未送達、unoffered custody，亦交獨立 closer。
4. `shutdown_phase1` 將 parked handle 交 closer，保留原始排空目標；Phase 2 以帳本配對回條結算，所以不遺漏成功項、不把移交當成成功，也不被 close 本身阻塞。`close_session_custody_for(Shutdown)` 同樣有界。
5. `close_session_preopened`／`close_before_bridge` 共用回條結算；`finish_worker_op` 保留原錯誤，失敗不再只寫泛稱。ledger 的 WorkerStopped／panic 終態也匯入 `settle_close`。
6. `spawn_release_claim_closer` 合併到共用 closer。無法建立 closer 時保留 handle 所有權，輸出 Failed 未知終態並永久阻擋；不退回 manager、actor 或回收者同步 close。
7. 結算後通知改為 `try_send`；終態保留供 CustodyPoll 補收。warning sink 在鎖外執行。

**熱插的既有同步邊界**：`update_devices` 以及 `close_session_custody_for(Device)` 沿用 Python 的鎖外同步關閉順序，通知返回時已關閉（既有凍結節點要求）。這些路徑不在 manager 鎖或回收者上執行 close。所有已持有 handle 的 attach／open 工作者也保留鎖外收尾。沒有改寫這些既有斷言。

**T14 分類**：新增 `ReaperCmd::RunJob` 為資源收尾命令，Open／Pending／Permanent 都執行；不經 actor admission、不啟動 attach／PROBE／登入，只分派已認領的關閉或排空工作。closer 與終態結算也都是三態下允許的資源收尾。未新增 RPC、SessionRequest、actor ControlEvent 或 Effect。

## 先紅後綠與端到端證據

起點 production code 未修時，新增測試與純 test-fakes 的 Held 注入入口，先取得下列 5 個行為紅燈（不是編譯失敗）：

| 測試 | 起點紅燈 | 修正後 |
|---|---|---|
| `wp510r_i20_actor_spawn_failure_close_pause_keeps_manager_responsive` | query Timeout（close 暫停期間 manager 鎖被佔住） | query／其他 session release／shutdown 各小於 1 秒；shutdown 列未排空；放行後 close 一次、原 token 終態 Closed |
| `wp510r_i20_held_close_pause_does_not_block_other_com_receipts_or_shutdown` | 另一 COM terminal 為 None | Held 暫停期間另一真 IoWorker 已處理 RX barrier 與 WorkerStopped，Closed＋Incomplete 證據；shutdown 只把仍關閉中的 Held 列 stalled |
| `wp510r_i20_reaper_held_close_failure_warns_once` | 告警 0 筆 | 原錯誤的 Failed 終態、CloseFailed、unconfirmed、close 一次、告警恰好一筆 |
| `wp510r_i20_reaper_unadopted_worker_close_failure_warns_once` | 告警 0 筆 | 同上，且不以 Complete 證據旗標跳過 close failure 告警 |
| `wp510r_i20_shutdown_parked_close_pause_respects_budget_and_late_receipt` | shutdown Timeout | budget 250 ms、小於 1 秒回覆 stalled；遲到回條依原 token Closed、close 一次 |

紅燈原文位於 `i20/red-spawn.log` 與 `i20/red-custody.log`。既有 `test_dr9_actor_spawn_failure_via_manager_path` 保留全部斷言並補上實際 `<1 s`；新建構失敗案例以 close pause 覆蓋交錯。

第六個新增節點 `wp510r_i20_rpc_query_and_release_while_reaper_held_close_paused` 是 ServiceRig 接線證據：Held 關閉暫停時，正式 `session.list` 與 `device.release COM0` 在 1 秒內完成。以 `dumps(..., RPC_WIRE)` 比對完整 release 回應（含 session、closed_consoles、aborted_cmd、version），COM0 的真正 IoWorker 關閉一次，Held 仍等自己的配對回條；放行後 Held close 一次、終態 Closed。

呼叫路徑：`Service::dispatch("device.release")` → `route_with_selector` → actor 的 Release／停止機制 → `ManagerExec::execute_with_feedback` → 帳本 ClaimForStop／ClaimByIdForRelease 或 `BeginDrain` → 關閉工作者／IoWorker finisher → `finish_custody_close` 或回收者 `handle_worker_stopped` → `CustodyLedger::settle_close` → watcher／CustodyPoll → actor 完成 RELEASED 與持久化。建構失敗分支則由 `create_session_locked` → `RunJob` → `dispatch_custody_job` → 相同 closer／終態出口。

## 驗收結果與實際限制

所有命令均在本工作樹前景執行並等結束。原始輸出與 JSON 摘要位於 `target/s2-par/wp510r/i20/`。

| gate | 結果 |
|---|---|
| 受影響 core，含 test-fakes | 1716 passed、0 failed、1 ignored（完整 workspace 亦驗證最終 source） |
| 受影響 daemon，含 test-fakes | 388 passed、0 failed、6 ignored（完整 workspace） |
| wp510 RPC focused | 78 passed、0 failed；含新 ServiceRig 案例 |
| workspace 含 test-fakes，`--no-fail-fast` | **FAIL：3441 passed、26 failed、51 ignored**；26 項與起點 baseline 重現逐名完全相同 |
| workspace 不含 test-fakes，`--no-fail-fast` | **FAIL：1629 passed、26 failed、46 ignored**；同一組 26 項 |
| 起點 CLI 三檔重現 | **FAIL：117 passed、26 failed、3 ignored**。以 `git show f661291b:<path>` 暫時回復本輪改過的 production source；確認所有 crate production source 的 diff 為空再跑，完成後逐檔還原本輪內容 |
| `cargo build --workspace --all-targets` | exit 0；對照起點，新增警告 0 |
| `cargo clippy -p sw-core -p sw-daemon --all-targets --features test-fakes --no-deps` | exit 0；對照起點，新增警告 0 |
| `cargo check --workspace --all-targets --target x86_64-pc-windows-gnu` | exit 0；對照起點，新增警告 0；僅交叉編譯 |
| cfg boundary | exit 0 |
| contracts check | ok=true、findings 0 |
| L1 add-only／check，基準 f661291b | s21_core.linux 新增 6、移除 0；結構與基準比對通過 |
| changelog fragment pytest | 1 passed；見下述安全邊界 |
| policy check | exit 0：25 PASS、2 WARN、0 FAIL；R-19 既有 CI gate 解析警示，R-22 既有 3216 筆懸空引用 advisory（不改範圍外 CI／文件） |

警告比對記錄 `warning-comparison.json`：以 `(warning 文字, source file)` 計數，排除行號位移與重複摘要；build 18／Clippy 61／Windows check 32 種既有診斷，新增皆為 0。未以「command exit 0」冒稱零警告。

Python 僅跑純檔案 fragment 測試：`python3 -m pytest --noconftest -q tests/test_changelog_fragments.py --basetemp target/s2-par/wp510r/i20/pytest-tmp`。明確不載入會探測 production daemon／設定／狀態的 `tests/conftest.py`；該測試只讀本 repo fragment、在 basetemp 建空目錄。因 marker 定義在 conftest，留下 2 筆 UnknownMarkWarning，沒有改案例判定。

**暫存路徑的驗收修正**：最初深層 TMPDIR 造成 daemon_process 23 項 SUN_LEN／READY 失敗；改工作樹根後，案例序號達三位數又使 socket 長度達 108 bytes。短相對路徑另有 `release_kill_hook_env_isolated_only` 的 wait_socket 逾時，未另行歸因。最終以 `/proc/<前景 shell PID>/cwd/target/t` 指向本工作樹的 `target/t`，固定短絕對路徑且不隨子行程 cwd 變動；沒有修改測試程式或架設 namespace／runner。完整 workspace 最終 daemon_process 33／33；先前失敗原文保留於 `test-sw-daemon.log`、`test-daemon-process-*.log`。

驗收途中另有原本要求「熱插通知返回時已 close」與「shutdown 成功列出 parked attach 目標」的凍結斷言紅燈，已修正實作後於最終完整 workspace 通過，未改斷言。所有 ignored 保持 ignored；硬體／擁有者項目維持 BLOCKED-OWNER。沒有原生 Windows、com0com 或真實 UART 實跑證據。

回歸歸屬評估（政策 155）：關閉卡住、失敗回條、鎖與跨 COM 結算都能以記憶體 serial 替身、真 IoWorker 與 ServiceRig 確定性覆蓋，沒有本項必須依賴實機的行為，因此不新增硬體 `regression/` case。

## 不成立主張

無。r3 三項 Medium 均接受並修正。

## SPEC-GAPS

本輪無須新增契約或擁有者裁決。已知 S24 Rust daemon 黑箱 5 項既有失敗，本輪未改 attach 回應形狀，未重跑該檔；保持既有缺口。未修改 Python 基線、contracts／golden、judge 或其他分支。

## 既有碼待辦

workspace 的 26 項 CLI 失敗已在起點 f661291b 重現：mgmt_doctor 6、mgmt_layout 1、mgmt_setup 19。保留 FAIL，不在 wp510r 關閉根因範圍內修復；`baseline-source-diff.log` 為空，`baseline-cli.log` 與 `test-summary.json` 保存來源與逐名相等證據。失敗 id 如下：

- `locate_manifest_env_override_first`
- `overall_ok_ignores_advisory_failures_only`
- `python_runtime_nonzero_rc_is_unrunnable`
- `pyyaml_import_failure_is_not_installed_with_fix`
- `runtime_spawn_error_is_unrunnable_not_not_installed`
- `runtime_timeout_is_unrunnable_not_not_installed`
- `s1m241_assessment_is_omitted_when_initial_ping_is_unreachable`
- `s1m241_assessment_is_omitted_without_installed_fingerprint`
- `s1m241_assessment_matching_ping_is_reported_once`
- `s1m241_assessment_restart_after_unreachable_is_reported`
- `s1m241_assessment_stale_systemd_pending_sudo_is_reported`
- `s1m241_assessment_stale_systemd_restart_failure_is_reported`
- `s1m241_assessment_stale_systemd_restarts_and_verifies`
- `s1m241_doctor_code_mismatch_is_advisory`
- `s1m241_failure_after_mismatch_preserves_python_stderr_and_exit_zero`
- `s1m241_failure_after_unreachable_preserves_python_stderr_and_exit_zero`
- `s1m241_failure_restart_error_preserves_python_hint_without_postcheck`
- `s1m241_failure_retries_unreachable_then_reports_match`
- `s1m241_setup_stale_daemon_without_fingerprint_is_restarted`
- `s1m241_setup_stale_service_is_restarted_and_verified`
- `s1m241_setup_system_scope_with_sudo_restarts_after_enable`
- `s1m241_skips_decision_priority`
- `s1m241_skips_flashing_without_force_before_materialize`
- `s1m241_skips_no_restart_flag`
- `s1m241_skips_no_restart_flag_preserves_mode_transition`
- `s1m_c1_setup_verify_polls_reconciled_socket`

## REMAINING

REMAINING: none
