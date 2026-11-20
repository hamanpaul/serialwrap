# event_latency 第 2 次獨立審閱（approved-with-conditions）條件 C1～C5 的完成證據（P5 第 7 輪）

- 審閱報告：`origin/docs/parity-amendments:docs/rust-refactor/reviews/p5-cloud/event-latency-method-review-2-20261029.md`
- 編排者指示（2026-10-29 21:35）：條件全部完成後 `freeze_status` 先維持 `pending_review_record`，三個審閱欄位依報告先填入；編排者核對後才改 `frozen`。
- 環境：雲端 Linux 4 核、一般使用者（uid 1000）、Rust 1.96.1；變異以 `run_mutation.sh` 於工作樹原地套用、跑完還原（每個變異均重新編譯）。

## 條件對照

| 條件 | 內容 | 落點 |
|---|---|---|
| C1 | drain_receipt 新界限識別字與明確定義（N_pre、界限式、跨 epoch／等待後才到者計入「＋1」）；`test_slice21_scope.py` 同步、其餘四事件不變；method_revision 維持 `2026-10-29.2`（外部口徑未改） | `contracts/resource_limits.yaml` drain_receipt 列 `l1_bound: at_most_pre_dependency_data_plus_one_after_receipt`、`l1_bound_rule`、`l1_bound_definition`；`tests/test_slice21_scope.py::test_event_latency_contract_values`／`::test_event_latency_od64_od65_records` |
| C2 | 探針記錄 N_pre（只計同 epoch），`ProbeEntry` 暴露；gate 斷言 `data_while_waiting ≤ N_pre＋1`（無依賴＝0） | `crates/sw-core/src/mailbox.rs`（`Witness.stopped_pre`、`note_stopped_push(epoch)`、`take_control_pre_dependency_data()`）、`crates/sw-core/src/actor.rs`（`ProbeEntry::pre_dependency_data`）；`drain_release_e2e::assert_drain_receipt_within_od64_bound`；`mailbox::{stopped_receipt_records_pre_dependency_data, pre_dependency_data_excludes_later_and_other_epoch_data, receipt_without_dependency_has_zero_pre_dependency_data, actor_probe_records_pre_dependency_data_for_drain_receipt}` |
| C3 | actor 層依賴情境 gate（在途命令 OpFinished 帶 after_rx_seq＋有限 backlog），release／shutdown 各一，斷言確實走到 `stopped`（N_pre ≥ 1）並符合界限；加入 drain_receipt `l1_gate` 並 add-only 登記 s21_core.linux；附變異 | `drain_release_e2e::drain_receipt_dependency_wait_on_{release,shutdown}_within_od64_bound`（各 20 輪、backlog 3～7 筆同 epoch＋前置 1 筆舊 epoch，斷言 N_pre＝backlog、見證數＝backlog＋1、OpFinished 先於 receipt）；變異 M3／M6／M7 |
| C4 | 每標籤（Release、CancelQueued、LeaseExpiry、Shutdown、WorkerStopped）至少重複 20 次；L-10 lane 見證數正向自測；M1／M5 變異紀錄 | `drain_release_e2e` 的兩支 flood gate 改為 `L1_GATE_REPEATS = 20` 輪（release／cancel／lease／shutdown 各 20 輪）；`mailbox::lane_witness_counts_data_returned_via_stopped_and_pending_paths`（stopped 與 pending 路徑各回傳 3 筆、見證數恰為 3）；變異 M1／M2／M5 |
| C5 | OD-65 凍結紀錄（Python r6 idle release 255.4 ms、Rust r6 最大 42.7 ms、維持 250／300、`retest_before` 保留）；L-1：stop_request 外部值止於 accept_point | `contracts/resource_limits.yaml` release_request 註解＋`freeze_note`；stop_request 註解＋`external_scope_note`；`test_event_latency_od64_od65_records` |

## 變異結果（各檔為原始輸出）

| 變異 | 內容 | 結果（修正後的 gate） | 審閱報告中修正前的結果 |
|---|---|---|---|
| M1 | data 可越過 control 至多 3 筆 | 5/5 紅：兩支 flood gate＋兩支依賴 gate（`M1.txt`） | 12/12 紅，但單看 Release 標籤 4/12 |
| M2 | 拿掉 lane 見證數累加 | `drain_release_e2e` 綠、`mailbox::lane_witness_counts_data_returned_via_stopped_and_pending_paths` 紅（`M2.txt`；該測試已列入 stop／cancel／release／drain_receipt 的 `l1_gate`，gate 不再靜默） | e2e gate 10/10 綠（靜默） |
| M3 | `stopped` 非空即 panic | 兩支依賴 gate 紅（`M3.txt`） | 兩支 l1_gate 綠（從未走到 `stopped`） |
| M5 | timer 每 4 輪才檢查＋deadline 過後仍回最多 3 筆 data | 5/5 紅：`release_handled_within_one_data_item_under_flood`（lease 段 20 輪）（`M5.txt`） | 1/6 紅 |
| M6 | `stopped` 路徑不先排空已讀 data（receipt 越過已讀 RX，與 Python 排空順序不符） | 兩支依賴 gate 紅（`M6.txt`） | — |
| M7 | N_pre 不分 epoch | 兩支依賴 gate 紅（`M7.txt`） | — |

## 不在本輪修改的審閱 Low（依收斂規則）

L-2～L-9 未修（L-1 的註記與 L-10 的自測屬條件 C5／C4，已完成）。
