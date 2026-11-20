# Slice 2.1 L1.5 情境與 P2 runner 的表達缺口（P5-T21）

本檔記錄 `func-test/scenarios/s21-*.yaml` 以 P2 runner 可表達與不可表達之處，以及第三輪（2026-10-28）L1.5 A/B 實跑結果。
裁決前，仍有缺口的情境即使 runner PASS 也不得當成完整證據：gate 第 9 段在 `slice21_gate_expect.yaml` 仍列有
`PA-OD-L15-*` 待裁決項時把 `l15_ab` 記為 EVIDENCE_MISSING（不計 PASS；審查 c0d221f #3）。不得以放寬斷言或近似情境取得 PASS。

## 第三輪 A/B 實跑（Linux PTY／bridge，cloud）

- 命令：`SERIALWRAP_GATE=1 SERIALWRAP_L15_SELFTEST_REPORT=<selftest-report.json> SERIALWRAP_L15_PY_DAEMON=tests/contract_blackbox/bin/py-serialwrapd
  SERIALWRAP_L15_RUST_DAEMON=target/x86_64-unknown-linux-gnu/release/serialwrapd python3 -m pytest -q tests/acceptance/l15/test_slice21_l15.py -k <案例>`
  （逐批執行；Rust release sha256 `2ea74890…c509`，見 `reviews/p5-t20-blackbox-report.md`）。
- bridge 自測：`python3 -m uart_stdio_bridge selftest --endpoint posix-pty --transport-mode virtual_pty`，tool tree sha256
  `9eaed6de…cd1c` 等於 `APPROVED_REVISION.json`，`evaluate_bridge_eligibility` → `('PASS', True)`。

| 情境 | Python | Rust | 說明 |
|---|---|---|---|
| P2 凍結 11 個：`l15-half-prompt`、`-slow-reader-backpressure`、`-transport-disconnect`、`-multi-source-contention-cli`、`-stale-event-delay`、`-reject-zero-tx`、`-exclusive-open`、`-reboot-min-guard`、`-release-inflight` | PASS | PASS | — |
| `l15-delayed-reply`（P2 凍結） | PASS | **FAIL** | 步驟 8 submit 回 `SESSION_NOT_READY`：PROMPT_TIMEOUT 後 Python 由 readiness reprobe（WAL：Ctrl-D 後 2.7 s `TX system \n`）回 READY，Rust 無 reprobe（08 計畫 P7-T5）。同 U-01，裁決項 `PA-OD-REPROBE` |
| `l15-wal-interference`（P2 凍結） | INSUFFICIENT | INSUFFICIENT | runner 兩邊 PASS，但 `compare: none` 不產 `comparison.json`（#7）；`PA-OD-L15-WAL` |
| `s21-credentials-unresolved`、`-exclusive-conflict`、`-exclusive-conflict-plain-open`、`-partial-tx-abort`、`-quiet-zero-tx`、`-release-attach-handoff`、`-wire-equals-wal` | PASS | PASS | 其中仍有斷言缺口者見下表 |
| `s21-reboot-minimal-guard` | PASS（修正後） | **FAIL** | 原情境在 READY 後立即 submit，兩邊皆回 `AUTOBOOT_QUIET`（`ready_reconfirm_pending:true`、`retry_after_s:5`）。依 #162 改為先等 `ready_reconfirm_pending == false`（模擬基線行為，未放寬）：Python PASS；Rust 永不送再確認 probe（同屬 readiness tick），30 s 逾時。`PA-OD-REPROBE` |
| `s21-wal-unlink-under-traffic` | FAIL | FAIL | `comparison.json` `HARNESS_STREAM_MISMATCH`（WAL 51 筆、wire 135 筆）：外部 unlink 把窗口前的 WAL 紀錄一併刪除，`until_op` 比對無 WAL 可比（#10）；**OD-46：移出 l15_ab** |
| `s21-event-latency` | INSUFFICIENT | INSUFFICIENT | 步驟 1 即 `daemon status 無 test_fakes.event_latency.release_request 計時欄位`：兩個 daemon（含 Rust test-fakes build）皆未提供（#6） |

## 缺口逐項處置

| # | 缺口 | 處置 |
|---|---|---|
| 1 | `s21-exclusive-conflict*`：steps 在 `harness.start()` 之後才執行，無法在 daemon 啟動前 `external_open`；初次自動 attach 的 `ATTACH_FAILED:DeviceHeldError` 與「全段 serial_read 0 byte」無法斷言 | **OD-39 已補**：情境新增 `pre_daemon_steps`（harness 於 DUT／bridge 就緒後、daemon 啟動前執行；只接受 sleep／op_begin／external_open／bridge_control／dut_control），兩個 exclusive 情境改為計畫原文前置並斷言 `ATTACH_FAILED:DeviceHeldError`、`assert_wire_offsets serial_read == 0`（起點快照無法 settled，見 CHG-S21-OD39-RUNNER-01）；原「release 後占用」段保留為第二段。Python／Rust 皆 PASS |
| 2 | `DEVICE_STILL_HELD.pids` 含外部行程 pid 無法斷言 | **已補**：expect 新增 `{list_contains: v}`（list 元素字串相等；`func-test/tests/test_s21_actions.py::test_expect_list_contains_matches_expanded_scalar` 先紅後綠）；兩個 exclusive-conflict 情境三處 attach 斷言加 `pids: {list_contains: "{ext.pid}"}`，Python／Rust 皆 PASS |
| 3 | `s21-credentials-unresolved`：daemon 啟動到第一個 step 之間的 TX 不在零 TX 窗口內 | **OD-39 已補**：窗口於 `pre_daemon_steps` 起算，零 TX 以 `assert_wire_offsets` 與 `assert_wal_window`（TX）判定。Python／Rust 皆 PASS |
| 4 | `s21-partial-tx-abort`：BC-03 條件式映射與「該 cmd_id TX len 總和 < 16001」無法表達 | **OD-39 已補**：`assert_wal_tx_total`（含歸檔 WAL 的 cmd_id TX 總和、比較子、save_as）＋`assert_when`／`assert_value`：TX>0 ⇒ `COMMAND_ABORTED_IN_FLIGHT`、=0 ⇒ `FLUSHED_BY_RECOVERY`。Python／Rust 皆 PASS |
| 5 | `s21-reboot-minimal-guard`：`recovery_action == EXPECT_REBOOT` 或 `done` 的 OR、`dut_generation == 2` | **OD-39 已補**：`assert_any`（只接受無副作用的檢查動作）與 `query_dut_generation`（wire.ndjson 最大 dut_generation）；情境已加，Python PASS。本情境依 OD-34 延至 Slice 2.3 |
| 6 | `s21-event-latency`：缺 `stop_request`、flood 無速率控制且量測時 flood 已結束（審查 c0d221f #1）、loadavg 超標不重跑、樣本數超過 900 s、兩個 daemon 皆無計時欄位、INSUFFICIENT_EVIDENCE 與 FAIL 共用 StepError（2af2554 #2） | **OD-45 已裁決 (a)（取代 OD-41 (b)）**：(1) 「控制事件不被資料擠掉」以 L1 結構測試為 gate，五事件皆保留 `at_most_one_data_item_before_control`（`drain_release_e2e::release_handled_within_one_data_item_under_flood`〔release／CancelQueued／LeaseExpiry／WorkerStopped〕、`…::shutdown_and_drain_receipt_handled_within_one_data_item_under_flood`〔Shutdown／WorkerStopped〕）；(2) 外部只量 stop_request、command_cancel、release_request，口徑「RPC 送出 → RPC 回應」，idle 與 flood（20000 B/s）各一輪（`scripts/rust/event_latency_measure.py`）；(3) lease_expiry、drain_receipt 只留 L1；(4) `method_revision` 2026-10-29.1，上限值待獨立審閱重新凍結（`freeze_status: pending_review_record`）。不在 daemon 加計時欄位、不改 WAL。**未結（OWNER-BATCH）**：`s21-event-latency` L1.5 情境仍以 daemon 計時欄位量五事件，OD-45 下兩個 daemon 皆無此欄位而恆 INSUFFICIENT；且 stop_request 每樣本須重啟 daemon，單一 bridge 情境無法取得 500 樣本。建議比照 OD-40／OD-46 自 `l15_ab` 逐名移出、由 `event_latency` section 承擔；裁決前不自行移出 |
| 7 | P2 凍結 `l15-wal-interference` 為 `compare: none`，兩個 label 恆 INSUFFICIENT | **OD-40 已裁決**：自 `l15_ab` 判定逐名移出（情境檔與 2.1 必要案例保留），由 `slice21_gate_expect.yaml` `l15_ab_excluded` 逐名列出的 pytest 承擔（unlink、reset 歸檔改名、reader-hold 為新增黑箱 A/B；寫入門檻輪替由 Python／Rust 單元測試承擔；Windows 讀取鎖定 LOCAL-WINDOWS-TODO），judge 要求每筆在指定 section passed |
| 8 | `external_open` 只能排在 release 之後（daemon 持 TIOCEXCL 時 EBUSY 依 R0-F3 判 FAIL） | **OD-39 已補**（同 #1，`pre_daemon_steps`） |
| 9 | 「從未進入 ATTACHING／READY」只能以 `wait_cli_until` 單點取樣（審查 c0d221f #2） | **OD-47 已裁決（CHG-S21-OD39-GAP9-01 APPROVED）**：核准偏離 OD-39 字面——兩個 daemon 皆不寫狀態轉移 META，`s21-exclusive-conflict`／`-plain-open` 以三層替代證據判定：WAL 窗口內 COM0 無 RX／TX（`assert_wal_window`）、bridge 端 `assert_wire_offsets serial_read == 0`、關鍵點狀態取樣。判定語意改為「外部放手前埠未成功開啟且未進 READY」（Python 基線 attach 失敗前本就短暫經過 ATTACHING，不列判定）；不在任一 daemon 新增狀態 META、不改 WAL 格式。情境註解已同步 |
| 10 | `s21-wal-unlink-under-traffic`：unlink 後 WAL 無窗口前紀錄可比 | **OD-46 已裁決 (c)**：比照 OD-40 自 `l15_ab` 判定逐名移出（情境檔保留；`tests/acceptance/l15/test_slice21_l15.py` `L15_AB_EXCLUDED`），列入 `slice21_gate_expect.yaml` `l15_ab_excluded`，改由黑箱 A/B `test_safety_core.py::test_wal_unlink_under_sustained_traffic_keeps_evidence` 承擔：unlink 期間 DUT 持續送 RX、agent 持續送命令 TX，以 unlink 前開啟的唯讀 fd 讀回舊 inode，斷言重建後證據不漏（判定 `wal_unlink_evidence_gaps`，紅燈證明 `test_redproof_wal_unlink_evidence_gaps`）。不改 `tools/uart_stdio_bridge`、不用硬連結（Rust 以 `nlink()==0` 偵測外部 unlink） |

裁決項：`PA-OD-L15-GAPS`（#1、#3～#5、#8）→ OD-39、#9 → OD-47、#10 → OD-46、`PA-OD-EVLAT`（#6）→ OD-41→OD-45、`PA-OD-L15-WAL`（#7）→ OD-40、`PA-OD-REPROBE`（readiness reprobe 依賴）→ OD-34（`l15-delayed-reply`、`s21-reboot-minimal-guard` 延至 Slice 2.3 出口；見 `slice21_gate_expect.yaml` `deferred_to_s23`）。

另：`tests/acceptance/l15/test_slice21_l15.py` 的 P2 凍結清單依 `cases.lock.json` `slice_2.1` 為 11 個（計畫程式草稿列 8 個，
與凍結 lock 不符；repo 內 06 計畫第 4827、4842 行亦為 11 個），總項數 43。
