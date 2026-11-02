> 審查者：agy gemini-3.8-flash-high（代理 sol 單審位，擁有者 2026-11-02 指定）

正在執行 `policy_check`，我將等待執行完成通知。
已啟動 `python3 -m policy_check --repo .` 背景檢查，稍候回報結果。
已啟動 `python3 -m tools.contracts check`，稍候回報結果。
已啟動 affected Rust tests 建置與測試，稍候回報結果。
已啟動 `tests/test_l1_judge.py` 與 `tests/test_slice21_gate_judge.py` 測試，稍候回報結果。
已啟動 `py_parity_76_80` 集合測試，稍候回報結果。
## A 前輪 finding 核對

本輪 `8c0c942a..bedc8deb` 共 3 個 commit，前輪各 finding 所在的程式、測試、鎖與 runbook 狀態與累計輪數核對如下：

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 2 | High | 已修 `25ad390` | 2 | 未改。 |
| 3 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 4 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 5 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 6 | High | 已修 `25ad390` | 2 | 未改。 |
| 7 | Medium | 已修 `25ad390` | 2 | 未改。 |
| 8 | Medium | 已修 `25ad390` | 2 | 未改。 |
| P5-1393573-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-1393573-2 | Medium | 已修 `d8ac988` | 2 | 未改。 |
| P5-5cc4623-1 | High | 已修 `0a7d0929` | 3 | 未改。 |
| P5-5cc4623-2 | Medium | 依裁決不處理 | 3 | 延後裁決仍適用。 |
| P5-70f6ce7-1 | High | 已修 `8134806` | 2 | 未改。 |
| P5-3c4e051-1 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3c4e051-2 | Medium | 已修 `cca5ba8` | 2 | 未改。 |
| P5-3084388-1 | High | 已修 `2472f78` | 3 | 未改。 |
| P5-3084388-2 | Medium | 已修 `289f7573` | 4 | 未改。 |
| P5-3084388-3 | Medium | 已修 `464adec` | 1 | 未改。 |
| P5-2472f78-1 | Medium | 已修 `c4cfb96` | 1 | 未改。 |
| P5-4f8acdf-1 | High | 已修 `86b54d8` | 1 | 未改。 |
| P5-4f8acdf-2 | Medium | 已修 `86b54d8` | 1 | 未改。 |
| P5-4f8acdf-3 | Medium | 已修 `86b54d8` | 1 | 未改。 |
| P5-4f8acdf-4 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-4f8acdf-5 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-6 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-4f8acdf-7 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-86b54d8-1 | Medium | 已修 `b9ae1d2` | 1 | 未改。 |
| P5-86b54d8-2 | Medium | 已修 `b9ae1d2` | 1 | 未改。 |
| P5-86b54d8-3 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-86b54d8-4 | Low | 依裁決不處理 | 1 | 未改。 |
| P5-86b54d8-5 | Low | 依裁決不處理 | 1 | 未改。 |
| P5-b9ae1d2-1 | Medium | 已修 `1b83da9d` | 2 | 未改。 |
| P5-b9ae1d2-2 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-b9ae1d2-3 | Low | 已修 `5d5314e` | 1 | 未改。 |
| P5-5d5314e-1 | High | 已修 `1b83da9d` | 1 | 未改。 |
| P5-5d5314e-2 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-5d5314e-3 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-5d5314e-4 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-5d5314e-5 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-54dd588-1 | Medium | 已修 `8d824b3a` | 1 | 未改。 |
| P5-54dd588-2 | Medium | 已修 `cc25b01d` | 1 | 未改。 |
| P5-54dd588-3 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-54dd588-4 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-54dd588-5 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-54dd588-6 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-8d824b3-1 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-8d824b3-2 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-8d824b3-3 | Low | 依裁決不處理 | 1 | 同上。 |
| P5-cc25b01-1 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-cc25b01-2 | Low | 依裁決不處理 | 1 | low-backlog。 |
| P5-8c0c942-1 | Low | 依裁決不處理 | 1 | 前輪發現；編排者第 8 輪裁決收進 low-backlog，不擋放行。 |

---

## B 新發現

NO FINDINGS

### 審查範圍與驗證說明
審查 commit 範圍：`8c0c942a..bedc8deb`（3 個 commit，無 merge commit）：
1. `200c10fd` fix: 對齊 drain 回條與自身子行程 holder 掃描
   - **OD-69 對齊 Python**：在 `uart_bridge.rs` 中，`IoWorker::finish_async` 於 reader join 後，若有排入 data lane 的 RX（`queued_rx_seq > 0`），透過 safety channel 發送 `ControlEvent::DrainDataBarrier`，並有界等待 actor dispatch 達到該 watermark。逾時會記錄 `EvidenceGap` 後續送回條，無無限等待風險。
   - **Capture 與 WAL 順序**：`session_core.rs` 將原先在 Phase 1 立即 `detach_captures()` 調整至 `DrainDataBarrier` 處理時才執行（並在 `worker_stopped` 保留最終兜底），確保 reader 已讀入之 RX 在 capture 與 WAL 均先於 receipt 處理。`actor.rs` 保留舊 epoch 的 dispatch watermark（`processed.max(mark)`），避免 release 清除 `conn_epoch` 後 barrier 判定失真。
   - **OD-64 契約銜接**：回條送出時 data backlog 已排空，完全符合編排者裁決「採 receipt 送出前排空，不改契約」，不打破 OD-64 界限。
   - **OWNER-BATCH #4 自身子行程排除**：`crates/sw-pal/src/proc_holders/posix.rs` 增加 `process_tree_pids`，排除 daemon 自身整個進程樹，避免 fork→exec 窗口期繼承 tty 的子進程被誤判為外部 holder。
   - **event_latency 契約凍結**：`contracts/resource_limits.yaml` 將 `freeze_status` 設為 `frozen`，未更動量測方法與門檻。
   - **實測**：`CARGO_TARGET_DIR=/tmp/solrev-p5-target cargo test -p sw-core --features test-fakes --test drain_release_e2e --test io_worker --test lease_selftest` 全數通過（含新增之 `release_receipt_follows_queued_rx_capture_and_wal` 與逾時 gap 測試）。`cap_posix_pty__own_child_inherited_tty_is_not_foreign_holder` 實測通過。
2. `5d732744` test: 補齊 Slice 2.1 parity 集合與 lock 裁決
   - **OD-68 移除 Linux 列**：依裁決以 `regen_lock --allow-removal` 逐名移除 `s21_gate:cap_windows_native.linux`（2 筆）與 `s21_gate:cap_interactive_desktop.linux`（1 筆）。
   - **工具相容性修復**：`tests/acceptance/regen_lock.py` 修正 `parse_removal_approval` 以定位 `:linux:` / `:windows:` 邊界，正確解析含冒號之集合名稱（如 `s21_gate:*`）；並於全平台移除時清理空平台與空集合鍵。
   - **SPEC-GAP #1 新增集合**：獨立以 add-only 新增 `s21_gate:py_parity_76_80`（137 個 nodeid），`slice21_gate.sh`、`slice21_gate_judge.py`、`slice21_gate_expect.yaml`、`slice21_gate_report.schema.json` 皆同步新增該 section。
   - **實測**：`python3 tests/acceptance/regen_lock.py --check` 通過；比對基準 `8c0c942a` 之 `--allow-removal` 驗證完全精確對齊 OD-68 授權的三筆；`python3 -m pytest -q <s21_gate:py_parity_76_80 argv>` 137 passed 通過。
3. `bedc8deb` test: 修正 self-test drain 競態測試時鐘次序
   - 修復 `crates/sw-core/tests/lease_selftest.rs` 的 `selftest_probe_drain_race_golden`，調整為先等待 release 完成後再推動 FakeClock 讓 probe 逾時，根除測試時鐘飆速導致的間歇性 3s drain timeout 偽失敗（對齊編排者裁決 P6 第 7 輪 §11-1）。
   - **實測**：`lease_selftest` 測試通過（34 passed）。

全部相關檢查均通過：
- `python3 -m policy_check --repo .` 通過（25 pass, 0 fail, 2 warn）。
- `python3 -m tools.contracts check` 通過（`ok: true`, `findings: []`）。

---

## 既有碼待辦（不計入結論）

1. 前輪既有碼待辦 1～7 依然存在（包含 Python 3.13 下 R-16 等既有環境差異）。
2. `scripts/rust/slice21_gate.sh:13-14` 用法說明文字描述 `--evidence-run` 在未凍結時為 INCOMPLETE（3），未反映存在 FAIL section 時 exit 1 之邏輯，待文件巡檢時潤飾。

---

## 對裁決的異議（不計入結論）

無。

---

結論：可放行
