> 審閱者：雲端 Claude Opus xhigh（≠ owner；擁有者 2026-10-29 指定雲端 Opus 代理審查）；受審 commit：feature/rust-refactor-p5 @ 8c0c942（條件落點 67b7e26／335c958）；核對對象：`event-latency-method-review-2-20261029.md` 的條件 C1～C5

結論：條件全部滿足，可凍結

- 方式：唯讀。`/home/user/rev` 以 `git checkout --detach 8c0c942` 審閱，worktree 未改動。
- 執行身分：一般使用者 ubuntu（uid 1000），Rust 1.96.1（依 `rust-toolchain.toml`），Python 3.12＋pytest 9.2.0。
- 變異測試：在另一份 `git archive 8c0c942` 複本進行，每個變異使用獨立的 target 目錄。
- 自 `86b54d8` 起，`scripts/rust/event_latency_measure.py`、`slice21_gate.sh`／`slice21_gate_judge.py` 的 event_latency 段沒有變更（`git diff 86b54d8 8c0c942` 對這些段為空）。外部量測方法沿用上次的審閱結論。

## 逐條核對

| 條件 | 判定 | 依據（8c0c942） |
|---|---|---|
| C1 契約（OD-64） | 滿足 | **契約**：`contracts/resource_limits.yaml:91-101`，drain_receipt 改為 `l1_bound: at_most_pre_dependency_data_plus_one_after_receipt`。<br>• `owner_decision_ref: OD-64`。<br>• `l1_bound_rule` 載明保留 D,D,D,L,W 排空順序。<br>• `l1_bound_definition` 定義：`n_pre`（移入 `stopped` 當下 data lane 中同 epoch 的筆數）、`bound`（≤ n_pre + 1）、`counted_in_plus_one`（跨 epoch 或等待後才到的 data）、`no_dependency`（n_pre = 0）。<br>• 註解見 `:82-90`。<br>**測試**：`tests/test_slice21_scope.py:110-114` 只對 drain_receipt 換界限，其餘四事件不變；`:121-148` 為新增的 `test_event_latency_od64_od65_records`。<br>**method_revision**：維持 `2026-10-29.2`，涵蓋範圍記於 `:45-50`。 |
| C2 探針 | 滿足 | **記錄 N_pre**：`crates/sw-core/src/mailbox.rs:348-357` 的 `note_stopped_push(epoch)` 在移入 `stopped` 時記下 `remaining = data_rx.len()`。`:316-323` 依 FIFO 逐筆扣減，只把同 epoch 的筆數記為 `same_epoch`。`:432-435` 在 W 取出時交出 N_pre。lane 事件記 0（`:343`）。<br>**對外暴露**：`take_control_pre_dependency_data()` 在 `:255`；`ProbeEntry::pre_dependency_data` 在 `crates/sw-core/src/actor.rs:68`，接線於 `:302-303`。<br>**gate 斷言**：`drain_release_e2e.rs:311-321` 的 `assert_drain_receipt_within_od64_bound` 斷言 `data_while_waiting ≤ pre_dependency_data + 1`；flood gate 對 WorkerStopped 仍另斷言 ≤ 1（`:355-360`、`:430-433`）。 |
| C3 依賴情境 L1 gate | 滿足 | **新測試**：`drain_release_e2e.rs:461-534`，`drain_receipt_dependency_wait_on_{release,shutdown}_within_od64_bound`。<br>• 使用真 actor loop＋core，在途命令的 OpFinished 帶 `after_rx_seq`。<br>• 有限 backlog：每輪 3～7 筆，前面另放 1 筆舊 epoch data；各跑 20 輪。<br>• 斷言 N_pre = backlog（確實走到 `stopped`）、見證數 = backlog＋1、見證數 ≤ N_pre＋1（`:500-511`）。<br>**登記**：已列入 drain_receipt 的 `l1_gate`（`resource_limits.yaml:96-101`）；`l1_required.lock.json` 只有新增 7 列、刪除 0 列，`regen_lock.py --check` OK。<br>**變異**：M3、M7、M9 皆轉紅（見下表）。 |
| C4 偵測力 | 滿足 | **重複 20 次**：`L1_GATE_REPEATS = 20`（`drain_release_e2e.rs:326`），release／cancel／lease／shutdown 各跑 20 輪（`:331-341`、`:412-416`），涵蓋 Release、CancelQueued、LeaseExpiry、Shutdown、WorkerStopped 五個標籤。<br>**L-10 正向自測**：`tests/mailbox.rs:785-837`，`lane_witness_counts_data_returned_via_stopped_and_pending_paths`。<br>**變異**：M1 5/5 紅；M5 6/6 紅（上次審閱為 1/6）；只拿掉 lane 見證數累加時，該自測轉紅。P5 原始輸出見 `p5-evidence/evlat-review2-conditions-r7/`。 |
| C5 凍結紀錄 | 滿足 | **release_request**（`resource_limits.yaml:107-114`）：註解與 `freeze_note` 載明 Python r6 一輪 255.4 ms（另兩輪 154.4／157.7 ms）、Rust r6 最大 42.7 ms、維持 250／300，並保留 `retest_before: "0.5.0"`。<br>**stop_request**（`:69-73`）：註解與 `external_scope_note` 載明外部值止於 accept_point，不得引為停止延遲保證。 |

## 實跑紀錄（8c0c942）

- `cargo test --locked -p sw-core --features test-fakes --test mailbox --test drain_release_e2e`：
  - drain_release_e2e **12 passed**，含兩支依賴情境 gate 與兩支 20 輪 flood gate。
  - mailbox **22 passed**，含新增的 5 支探針測試。
- `pytest -q tests/test_event_latency_measure.py tests/test_slice21_scope.py tests/test_slice21_gate_judge.py`：**85 passed、1 skipped**。skipped 的是凍結測試，因 `freeze_status` 仍為 `pending_review_record`，屬預期。
- `python3 -m tools.contracts check`：`"ok":true`，findings 0。
- `python3 tests/acceptance/regen_lock.py --check`：兩項 OK。
- 契約五事件的 `l1_gate` nodeid 全部在 `s21_core.linux` 鎖內（逐筆比對，缺 0）。
- r7 證據 `event-latency-od45-rust-r7-261029.json`：
  - 8 支 l1_gate 全 passed；18 輪 × 500 筆全 PASS，errors／load_problems／void 皆為 0。
  - 各事件最大值：stop 5.79 ms、cancel 16.08 ms、release 76.32 ms，皆在上限內。
  - tool／contract sha256 與 8c0c942 的檔案相同。
  - verdict 為 INSUFFICIENT_EVIDENCE，唯一原因是尚未凍結。

## 變異結果（依審閱者自行撰寫的腳本，針對 8c0c942）

| 變異 | 內容 | l1_gate 結果 |
|---|---|---|
| 基線 | 不變異 | 2/2 綠 |
| M1 | data 可越過 control 至多 3 筆 | **5/5 紅**（兩支 flood gate＋兩支依賴 gate） |
| M2 | M1＋拿掉 lane 見證數累加 | **紅**（兩支依賴 gate；不再靜默） |
| M2b | 只拿掉 lane 見證數累加 | **紅**（`lane_witness_counts_data_returned_via_stopped_and_pending_paths`） |
| M3 | `stopped` 非空即 panic | **紅**（兩支依賴 gate；證明 gate 已走到依賴等待路徑） |
| M5 | timer 每 4 輪才檢查，且 deadline 過後仍回最多 3 筆 data | **6/6 紅**（`LeaseExpiry 等待期間處理了 3 個 data item`） |
| M7 | N_pre 不分 epoch | **紅**（兩支依賴 gate：N_pre ≠ backlog） |
| M8 | N_pre 把等待後才到的 data 也算進去 | **紅**（`pre_dependency_data_excludes_later_and_other_epoch_data`） |
| M9 | `stopped` 路徑不先排空已讀 data | **紅**（兩支依賴 gate） |
| M10 | `stopped` 路徑不先釋放同 epoch 依賴事件（W 先於 L） | 兩支依賴 gate 5/5 **綠**；`mailbox::stopped_receipt_records_pre_dependency_data` 不轉紅而是卡住（60 s 逾時）（見 N-1） |

## 是否引入新的假綠路徑

沒有發現新的假綠路徑：

- 界限放寬只作用於移入 `stopped` 的 receipt；無依賴情境的 flood gate 仍對 WorkerStopped 斷言 ≤ 1。若回歸讓 receipt 無故進入 `stopped`，這項斷言或飢餓逾時會抓到。
- 量測工具、gate、judge 的 event_latency 段未改。
- 新增的 nodeid 都屬於 `l1_gate` 的聯集，量測工具要求聯集內全部 passed（`overall_verdict`），缺任何一支即判證據不足。

以下為不阻擋凍結的觀察：

- **N-1（Low）依賴 gate 的「OpFinished 先於 receipt」檢查有競態**
  - 位置：`drain_release_e2e.rs:487-493`。
  - 現象：測試執行緒先等到 W 的紀錄出現，再數 Control 筆數；W 先於 L 的回歸（M10）下，L 在數微秒內就被處理，所以 5/5 仍綠。
  - 唯一抓到 M10 的 `stopped_receipt_records_pre_dependency_data` 用無 deadline 的 `recv_next`，結果是卡住而非 FAIL。gate 中 `cargo_l1_results.py` 會在 900 s 逾時後判未完成、nodeid 缺失，結果為證據不足，不是 PASS。
  - 建議：探針記錄全域處理序號，並以序號比對 L 與 W 的先後；mailbox 測試給 deadline，使其直接 FAIL。
- **N-2（Low）P5 回報與契約的小落差**
  - 第 7 輪回報 §2.4 稱 lane 見證數自測已加入「stop／cancel／release／drain_receipt」的 `l1_gate`，但 `release_request` 的 `l1_gate`（`resource_limits.yaml:110-114`）沒有這一支。
  - 因 gate 以聯集判定，功能上沒有缺口，屬文件更正。
- **N-3（Low）依賴 gate 是合成情境**
  - 依賴 gate 預先填好 mailbox，使用替身 executor；Release 走 control lane，與正式環境的 safety lane 不同。
  - 界限在正式環境成立，靠的是「reader join 後才送 W、之後不再有同 epoch data」這項 IoWorker 不變量；`stopped` 路徑本身排空 data 時不分 epoch（`mailbox.rs:399-402`）。
  - 違反時探針會揭露（`pre_dependency_data_excludes_later_and_other_epoch_data` 證明 w > N_pre＋1 可被看見），但 gate 中沒有真 IoWorker 的依賴情境。
- **N-4（Low）N_pre 讀取時點的微小競態**
  - `remaining = data_rx.len()` 在 receipt 取出之後才讀取（`mailbox.rs:354`）；此窗口內並行到達的 data 會被算進 N_pre，使界限略寬。
  - 正式環境中 reader 已停，不會有並行到達的 data；實際影響可忽略。
- **上次審閱的 L-2～L-9**：仍未修，依原分級屬 Low、不阻擋凍結。

## 凍結時注意

- 改 `freeze_status: frozen` 會變更契約 sha256，judge 將拒收 r7 證據（設計如此）。凍結 commit 之後須重跑 T22 gate（非 dry-run、非 root），取得新的 event_latency 證據，才能作為 Slice 出口依據。
- 審閱紀錄三欄維持 `reviewed_by: cloud-claude-opus-xhigh`、`reviewed_at: "2026-10-29"`、`review_conclusion: approved-with-conditions`，並在契約註解附上本檔路徑（`reviews/p5-cloud/event-latency-method-review-2-conditions-20261030.md`），作為條件核對完成的紀錄。
