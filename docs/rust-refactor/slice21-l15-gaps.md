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
| `s21-wal-unlink-under-traffic` | FAIL | FAIL | `comparison.json` `HARNESS_STREAM_MISMATCH`（WAL 51 筆、wire 135 筆）：外部 unlink 把窗口前的 WAL 紀錄一併刪除，`until_op` 比對無 WAL 可比（#10） |
| `s21-event-latency` | INSUFFICIENT | INSUFFICIENT | 步驟 1 即 `daemon status 無 test_fakes.event_latency.release_request 計時欄位`：兩個 daemon（含 Rust test-fakes build）皆未提供（#6） |

## 缺口逐項處置

| # | 缺口 | 處置 |
|---|---|---|
| 1 | `s21-exclusive-conflict*`：steps 在 `harness.start()` 之後才執行，無法在 daemon 啟動前 `external_open`；初次自動 attach 的 `ATTACH_FAILED:DeviceHeldError` 與「全段 serial_read 0 byte」無法斷言 | **OD-39 已補**：情境新增 `pre_daemon_steps`（harness 於 DUT／bridge 就緒後、daemon 啟動前執行；只接受 sleep／op_begin／external_open／bridge_control／dut_control），兩個 exclusive 情境改為計畫原文前置並斷言 `ATTACH_FAILED:DeviceHeldError`、`assert_wire_offsets serial_read == 0`（起點快照無法 settled，見 CHG-S21-OD39-RUNNER-01）；原「release 後占用」段保留為第二段。Python／Rust 皆 PASS |
| 2 | `DEVICE_STILL_HELD.pids` 含外部行程 pid 無法斷言 | **已補**：expect 新增 `{list_contains: v}`（list 元素字串相等；`func-test/tests/test_s21_actions.py::test_expect_list_contains_matches_expanded_scalar` 先紅後綠）；兩個 exclusive-conflict 情境三處 attach 斷言加 `pids: {list_contains: "{ext.pid}"}`，Python／Rust 皆 PASS |
| 3 | `s21-credentials-unresolved`：daemon 啟動到第一個 step 之間的 TX 不在零 TX 窗口內 | **OD-39 已補**：窗口於 `pre_daemon_steps` 起算，零 TX 以 `assert_wire_offsets` 與 `assert_wal_window`（TX）判定。Python／Rust 皆 PASS |
| 4 | `s21-partial-tx-abort`：BC-03 條件式映射與「該 cmd_id TX len 總和 < 16001」無法表達 | **OD-39 已補**：`assert_wal_tx_total`（含歸檔 WAL 的 cmd_id TX 總和、比較子、save_as）＋`assert_when`／`assert_value`：TX>0 ⇒ `COMMAND_ABORTED_IN_FLIGHT`、=0 ⇒ `FLUSHED_BY_RECOVERY`。Python／Rust 皆 PASS |
| 5 | `s21-reboot-minimal-guard`：`recovery_action == EXPECT_REBOOT` 或 `done` 的 OR、`dut_generation == 2` | **OD-39 已補**：`assert_any`（只接受無副作用的檢查動作）與 `query_dut_generation`（wire.ndjson 最大 dut_generation）；情境已加，Python PASS。本情境依 OD-34 延至 Slice 2.3 |
| 6 | `s21-event-latency`：缺 `stop_request`、flood 無速率控制且量測時 flood 已結束（審查 c0d221f #1）、loadavg 超標不重跑、樣本數超過 900 s、兩個 daemon 皆無計時欄位、INSUFFICIENT_EVIDENCE 與 FAIL 共用 StepError（2af2554 #2） | **OD-41 停下回報（BLOCKED-OWNER）**：OD-41 要求以「觸發 RPC 時間 vs WAL 對應 META 的 mono_ts_ns」外部量測，並先確認五種事件在 WAL 皆有可對應紀錄。實測（Python 基線與 Rust release，同一 session）：兩邊 WAL `mono_ts_ns` 與 client `time.monotonic_ns()` 同時鐘可比；但 `stop_request`（daemon.stop）、`command_cancel`（在途命令 cancel）、`lease_expiry`、`drain_receipt`、`release_request`（idle release）**五種事件在兩個 daemon 的 WAL 皆無對應紀錄**（Python 只在 release 中止在途命令時寫 `command_aborted_in_flight`，Rust 同情境亦無）。依裁決「缺者停下回報」，未實作量測、未改 daemon（不新增計時欄位或 META）。需擁有者裁決：新增對外 WAL META（檔案格式變更，需 SC/BC 登記）或改以其他可觀測點 |
| 7 | P2 凍結 `l15-wal-interference` 為 `compare: none`，兩個 label 恆 INSUFFICIENT | **OD-40 已裁決**：自 `l15_ab` 判定逐名移出（情境檔與 2.1 必要案例保留），由 `slice21_gate_expect.yaml` `l15_ab_excluded` 逐名列出的 pytest 承擔（unlink、reset 歸檔改名、reader-hold 為新增黑箱 A/B；寫入門檻輪替由 Python／Rust 單元測試承擔；Windows 讀取鎖定 LOCAL-WINDOWS-TODO），judge 要求每筆在指定 section passed |
| 8 | `external_open` 只能排在 release 之後（daemon 持 TIOCEXCL 時 EBUSY 依 R0-F3 判 FAIL） | **OD-39 已補**（同 #1，`pre_daemon_steps`） |
| 9 | 「從未進入 ATTACHING／READY」只能以 `wait_cli_until` 單點取樣（審查 c0d221f #2） | **OD-39 部分承接**：新增 `assert_wal_window`（op 窗口的 CLOCK_MONOTONIC 對 WAL `mono_ts_ns`，窗口內指定 COM 不得有 RX／TX 紀錄）。偏離：OD-39 原文寫「以 WAL META 判定窗口內狀態序列」，但 Python 與 Rust daemon 皆**不寫狀態轉移 META**（實查兩邊 META 事件僅 command_completion、command_aborted_in_flight、tx_outcome_unknown、credentials_unresolved、transport_stall_suspected、WAL_RECREATED／ROTATED 等），故以「窗口內無 COM0 RX／TX」作為未開埠、未進 ATTACHING／READY 的必要證據；精確狀態序列需 daemon 新增 META（對外檔案格式變更）→ BLOCKED-OWNER |
| 10 | `s21-wal-unlink-under-traffic`：unlink 後 WAL 無窗口前紀錄可比 | **BLOCKED-OWNER（OD-39 #10 無法在不改 bridge 工具樹下實作）**：比較器只支援 `until_op` 前綴窗口，且輸入須通過 `verify_integrity`（雜湊／序號），runner 無法以過濾副本建立「WAL 重建後窗口」。可行替代需擁有者擇一：(a) 核准比較器新增 `since_op`／`since_wal_meta` 起點窗口（改 `tools/uart_stdio_bridge`，須重新核准 revision）；(b) 改為 runner 在 `wal_control unlink_current` 前以硬連結保存現行 WAL 為歸檔名（比較器已讀 `raw.wal.ndjson.*`），維持 `until_op:unlink-window` |

裁決項：`PA-OD-L15-GAPS`（#1、#3～#5、#8～#10）→ OD-39、`PA-OD-EVLAT`（#6）→ OD-41、`PA-OD-L15-WAL`（#7）→ OD-40、`PA-OD-REPROBE`（readiness reprobe 依賴）→ OD-34（`l15-delayed-reply`、`s21-reboot-minimal-guard` 延至 Slice 2.3 出口；見 `slice21_gate_expect.yaml` `deferred_to_s23`）。

另：`tests/acceptance/l15/test_slice21_l15.py` 的 P2 凍結清單依 `cases.lock.json` `slice_2.1` 為 11 個（計畫程式草稿列 8 個，
與凍結 lock 不符；repo 內 06 計畫第 4827、4842 行亦為 11 個），總項數 43。
