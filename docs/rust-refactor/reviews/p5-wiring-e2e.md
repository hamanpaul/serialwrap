# P5（Slice 2.1）接線＋端到端驗收（第五輪，2026-10-29）

依 `review-convergence-rules.md` §5：列出本 slice 新增的 pub fn／欄位、逐一找正式路徑的呼叫端，並以 daemon 級端到端測試證明
「把任一段改成 no-op 時測試會紅」。P5～P7 於 slice 出口前補做；本檔為 P5 第五輪的結果。

## 1. 清查方法

- 範圍：`git diff origin/feature/rust-refactor..HEAD -- crates`（P5 相對主線新增）中 `crates/*/src/**` 新增的 `pub fn`／`pub(crate) fn`（含 impl 方法）。
- 正式路徑：`crates/*/src/**`，排除 `#[cfg(test)]`／`#[cfg(feature = "test-fakes")]` 標註的項目，以及 `sw-pal/src/{fakes,conformance}/`（測試支援模組）。
- 工具：一次性清查腳本（ripgrep 等價的 regex 掃描；僅為輔助，結果逐筆人工複核），統計如下。

| 類別 | 數量 | 判定 |
|---|---:|---|
| 新增 pub fn 總數 | 418 | — |
| 有正式路徑呼叫端 | 307 | 接線完成 |
| 測試支援模組（`sw-pal/src/fakes`、`conformance`） | 59 | 測試替身，非產品介面 |
| `#[cfg(feature = "test-fakes")]`／`cfg(test)` 標註的觀測與注入點 | 31 | 只在 test-fakes build 存在（不進產品 binary） |
| 名稱或註解標示「只供測試」 | 1 | 同上 |
| 其餘（人工複核） | 20 | 見下表 |

### 1.1 其餘 20 個（人工複核）

| 函式 | 正式路徑 | 結論 |
|---|---|---|
| `sw-pal serial/windows_model::{dcb_model, dcb_flags, comm_timeouts_model}` | `serial/windows.rs:183` 等（Windows 後端開埠） | 已接線（清查腳本因檔頭 cfg 區塊誤判） |
| `sw-core state_store::load_state` | 產品走 `load_state_with_warn`（`sw-daemon/src/daemon_main.rs:1030`），兩者同一本體 | 包裝；測試用 |
| `sw-core self_test::classify_pre_probe` | 產品走 `SessionCore::self_test_pre_probe`（`session_core.rs:4858`），兩者同一本體 | 包裝；測試用 |
| `sw-core session_manager::on_tick` | Tick 由 manager thread 直接 `pump()`（`session_manager.rs:750`） | 包裝；測試用 |
| `sw-daemon daemon_main::resolve_test_allowlist` | 產品走 `resolve_allowlist_with_caps`（同檔） | 包裝；測試用 |
| `sw-core session_manager::com_for_by_id` | 無 | Python `SessionManager.com_for_by_id` 同樣無正式呼叫端（1:1 對外 API），保留 |
| `sw-core session_manager::wal_health_fanout`、`tx_writer::{try_with, ledger_gauge}`、`uart_bridge::idle_s`、`arbiter::{set_in_flight, physical_len}`、`wal::set_warn_sink`、`launcher::manifest_absent`、`path_norm::normalize_for_compare` | 測試觀測／注入點（未加 test-fakes 標註） | 不影響產品行為；是否收進 `test-fakes` 屬既有碼整理（既有碼待辦） |
| `sw-core session_manager::attached_real_paths` | 無（含測試） | 死碼（既有碼待辦，不在本輪修） |
| `sw-proto pycompat::{strip_ansi, shlex_quote}` | 無正式呼叫端（Python 對應函式供 Stage 2 後續 slice 使用） | 保留，非本輪範圍 |

結論：無「新增介面沒接上正式呼叫路徑」而影響對外行為的項目；兩個死碼／未標註觀測點列入既有碼待辦。

## 2. 本輪新增／變更的接線與 daemon 級端到端測試

| 變更 | 正式呼叫路徑 | daemon 級端到端測試 | no-op 變異（實測） |
|---|---|---|---|
| `op_runner::{WrapEvidence, match_wrapped_echo, strip_wrapped_command_echo}`（P5-70f6ce7-1） | `service.rs` `command.submit` → `SessionManager::route` → actor `StartOp` → `OpRunner::execute` → `extract_command_stdout` | `sw-daemon/tests/dispatch.rs::command_submit_line_mode_wrapped_echo_end_to_end`（ServiceRig：RPC 分派＋manager＋actor＋op runner＋FakeDevice） | (a) `strip_wrapped_command_echo` 結果丟棄（no-op）→ 紅（BusyBox 案 stdout 留回顯）；(b) 換行證據恆真（不看原始串流）→ 紅（一般 CRLF 案被誤剝）。還原後綠 |
| `Effect::Persist` 同步套 delta（P5-5cc4623-1，沿用 F156） | actor `Effect::Persist` → `ManagerExec::execute` → `model_apply` → 持久化工作者 `commit_sync_kind` | `sw-core/tests/session_manager.rs::release_commit_paused_then_bind_keeps_binding_without_stale_released_row`（ManagerRig：manager＋actor＋state store＋FakeSerial；含重啟實體化） | delta 改回工作者內套用 → 紅（舊 `Released{A}` 落盤）。daemon 行程級的 release→bind 交錯需 `before_release_commit` 暫停點，僅 test-fakes 可注入；`sw-daemon/tests/restart_after_commit.rs` 覆蓋 release 持久化後重啟 |
| test-fakes 探針 `Shutdown`／`WorkerStopped` 標籤（OD-45 L1 gate） | actor 主迴圈 `probe_label`（只在 test-fakes build） | `sw-core/tests/drain_release_e2e.rs::{release_handled_within_one_data_item_under_flood, shutdown_and_drain_receipt_handled_within_one_data_item_under_flood}` | 移除標籤 → 紅（探針無紀錄） |
| `scripts/rust/event_latency_measure.py`（OD-45 外部量測） | `scripts/rust/slice21_gate.sh` §9 `event_latency` section | 工具本身即 daemon 行程級（真 daemon＋FakeTarget PTY）；判定純函式 `tests/test_event_latency_measure.py` | `overall_verdict`／`judge_run` 各分支皆有反例（超限、L1 failed、未凍結、診斷模式、缺輪次、過載作廢） |
| `wal_unlink_evidence_gaps`＋持續流量黑箱（OD-46） | gate `blackbox_python`／`blackbox_rust`（`l15_ab_excluded`） | `test_safety_core.py::test_wal_unlink_under_sustained_traffic_keeps_evidence`（Python／Rust 真 daemon） | 判定為 stub → `test_redproof_wal_unlink_evidence_gaps` 紅；七種漏證據輸入皆紅 |

## 3. 未結

- Windows 原生（`windows_model` 等）的端到端驗收屬 LOCAL-WINDOWS-TODO（BO-S21-13）。
- 清查腳本為一次性輔助；Slice 出口整包審查時由審查者複核本表。
