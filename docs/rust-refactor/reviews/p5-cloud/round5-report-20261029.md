# P5（Slice 2.1）第 5 輪雲端回報（2026-10-29）

- 分支：`feature/rust-refactor-p5`（本輪唯一寫入者）；起點 `cca5ba8`，終點 `110bd58`。
- 開工時 `origin/feature/rust-refactor` 已為祖先，無需 merge；L1 鎖本輪只 add-only（`s21_core.linux` +5）。
- 環境：cloud Linux（4 核、ext4／virtio），容器以 root 執行。Rust 測試以非 root（uid 1000，`setpriv` 包裝的 `tcargo`）跑；
  gate 與全套 pytest 依前輪慣例以 root 跑，root 類失敗逐名附非 root 重跑證據。Python 3.12（PATH shim；容器預設 3.11 無法收集 `tests/test_flash_pump.py`）。
- 審查依據：`rulings/p5.md`（2026-10-29 版）、OD-45～OD-47、最新 sol 報告 `33863d5_cca5ba8`、本輪期間新出的 `cca5ba8_3084388`、`3084388_464adec`。

## 1. commit 清單

| commit | 內容 |
|---|---|
| `8134806` | P5-70f6ce7-1 折行回顯改依修正 PR 語意；P5-5cc4623-1 回歸補重啟實體化；OD-45 L1 補 Shutdown／WorkerStopped 結構 gate |
| `7e97d6b` | OD-46 WAL unlink 持續流量黑箱 A/B、移出 l15_ab；OD-47 CHG-S21-OD39-GAP9-01 APPROVED |
| `b8f25bc` | OD-45 event_latency 契約新 method_revision、外部量測工具、gate 接線；接線＋端到端驗收文件與 daemon 級測試 |
| `3084388` | 量測工具 stop_request 準備失敗另記重試；附第一版三輪實測（無身分、與全套 pytest 並行，後已取代） |
| `464adec` | gate：證據不足記 EVIDENCE_MISSING 而非 FAIL；證據去除本機路徑（R-21） |
| `0404175` | T22 gate 第五輪實跑紀錄（`r5b-261029`） |
| `2472f78` | sol P5-3084388-1／-2：取消被拒不計樣本、報告綁定受測身分 |
| `1864a6b` | 以 `2472f78` 乾淨工作樹重採三輪證據（後由 `110bd58` 取代） |
| `c4cfb96` | sol P5-2472f78-1（Python 3.10 雜湊相容）、P5-3084388-2 第 3 輪（綁定 artifact 建置來源） |
| `110bd58` | 綁定建置來源後重採三輪證據 `event-latency-od45-rust-evlat-r5-c4cfb96.json`；fix-responses F168／F169 |

每組皆跑（`0404175` 之後的 commit 只動量測工具、其單元測試與文件，未重跑全套 pytest；gate `r5b-261029` 的 pytest_full 於 `464adec5` 執行）：受影響 crate 的 `cargo test --features test-fakes`（非 root）、`cargo check --workspace --target x86_64-pc-windows-gnu`、觸及的 Python 測試、
`python3 -m tools.contracts check`、`python3 -m policy_check --repo .`（fail 0）、`python3 scripts/rust/check_cfg_boundary.py`。
**揭露**：`3084388` 提交前漏跑 policy_check，該 commit 的證據檔含本機絕對路徑使 R-21 FAIL（sol P5-3084388-3），已於 `464adec` 修正。

## 2. High／Medium 與裁決處理

| 項目 | 嚴重度 | 處理 |
|---|---|---|
| P5-5cc4623-1（rulings 改修法、最優先） | High | 實作沿用 `0a7d0929`（Persist 於 actor 順序同步套 delta，工作者只跑暫停鉤子並提交 commit 當下快照——等價於「依當下狀態重算、不重放失效 delta」）。本輪補裁決要求的重啟實體化：`session_manager::release_commit_paused_then_bind_keeps_binding_without_stale_released_row` 追加 restart＋bootstrap_attach，斷言非 RELEASED、binding B、無 released 列。紅燈：delta 改回工作者內套用即紅 |
| P5-70f6ce7-1（照修） | High | `op_runner::extract_command_stdout` 改為修正 PR `34c3a84` 的逐字元比對（`8134806`），並保留裁決要求的「CRLF 一般 stdout 不被剝」——換行插入須 raw 含 `\r\r\n`、空白插入須 raw 含 `" \r"`。新紅燈 2 案（舊實作 FAIL）；裁決列的 7 類回歸皆有案且綠。**oracle 變更揭露**：golden 案末段原依 842c014 斷言「殘留 prompt 不計入距離」，與裁決「第一個以首行欄位計」相反，改由新案以裁決語意斷言（fix-responses F163） |
| P5-3c4e051-1／-2 | Medium | 已於 `cca5ba8` 修正，sol `33863d5_cca5ba8` 標已修；本輪不另改 |
| P5-5cc4623-2 | Medium | 依裁決延後，未改 Python 折行段 |
| P5-3084388-1（本輪新） | High | 已修 `2472f78`：取消須 ok:true／canceled 且命令最終 canceled 才計樣本；紅燈 `test_cancel_sample_rejects_refused_cancel` |
| P5-3084388-2（本輪新，sol 累計 4 輪） | Medium | 已修 `2472f78`＋`c4cfb96`＋`110bd58`：報告 identity（run ID、daemon sha256、artifact 建置來源 commit／build run——以建置該 artifact 的 gate `report.json` 核對 sha256 後取得、量測工具 commit 與乾淨與否、工具與契約 sha256、method_revision），未綁定或不乾淨不得 PASS；證據重採為 `event-latency-od45-rust-evlat-r5-c4cfb96.json`（artifact＝gate `r5b-261029`＠`464adec5`，sha256 `8aae8fc3…`）。sol `2472f78_c4cfb96` 審查時點早於 `110bd58`，故仍標未修 |
| P5-2472f78-1（本輪新） | Medium | 已修 `c4cfb96`（sol 確認）：分塊 sha256；紅燈 `test_sha256_file_works_without_hashlib_file_digest` |
| P5-3084388-3（本輪新） | Medium | 已修 `464adec`（sol `3084388_464adec` 確認） |
| OD-45 | — | (1) L1 結構 gate：探針加 `Shutdown`／`WorkerStopped`，新增 `shutdown_and_drain_receipt_handled_within_one_data_item_under_flood`，release 案追加 WorkerStopped——五事件皆有 `at_most_one_data_item_before_control` 測試並寫入契約 `l1_gate`；(2) 外部只量 stop_request／command_cancel／release_request，「RPC 送出 → RPC 回應」，idle 與 flood 20000 B/s（`scripts/rust/event_latency_measure.py`）；(3) lease_expiry、drain_receipt 標 `l1_only`；(4) method_revision `2026-10-29.1`，上限值沿用、審閱欄位未自填，`freeze_status: pending_review_record`。未在 daemon 加計時欄位、未改 WAL |
| OD-46 | — | 新增 `test_wal_unlink_under_sustained_traffic_keeps_evidence`（unlink 期間持續 RX／TX；以 unlink 前開啟的唯讀 fd 讀回舊 inode，不用硬連結；判定 `wal_unlink_evidence_gaps`，紅燈證明 `test_redproof_wal_unlink_evidence_gaps`）；`s21-wal-unlink-under-traffic` 列 `L15_AB_EXCLUDED` 與 `l15_ab_excluded`。未改 `tools/uart_stdio_bridge`。Python／Rust 實跑 PASS |
| OD-47 | — | CHG-S21-OD39-GAP9-01 APPROVED；判定語意「外部放手前埠未成功開啟且未進 READY」；情境註解與 `slice21-l15-gaps.md` 同步；pending_decisions 清空 |
| 新裁決：P6 SPEC-GAP #4（非 root holder 掃描 fail-closed，P5 擁有） | — | 裁決指定「P5 第六輪」；本輪未動，列入下一輪最優先 |
| 新裁決：OD-55（`s21_core` 的 `#[ignore]` 能力列移入 `s21_gate:cap_<cap>`） | — | 裁決交付點「P5 第 6 輪（P6／P7 merge 後）」；本輪未動 |

## 3. T22 Slice 2.1 出口 gate（非 dry-run，`r5b-261029`，受測 `464adec5`）

verdict **FAIL**（exit 1）。首跑 `r5-261029` 因殘留的 throwaway Python daemon 作廢（見 `p5-t22-gate-run.md`）。

| section | 判定 | 數字 |
|---|---|---|
| cfg_boundary | PASS | — |
| linux_l1 | FAIL | 1719／7／28；7 支 root 證據不足，非 root 重跑全綠（F108） |
| linux_cap_posix_pty | EVIDENCE_MISSING | 347／4／8（集合未登記） |
| windows_native_l1 | EVIDENCE_MISSING | 無原生 Windows |
| windows_cap_windows_com | EVIDENCE_MISSING | BO-S21-01 |
| blackbox_python | EVIDENCE_MISSING | 43／0／6（集合未登記） |
| blackbox_rust | EVIDENCE_MISSING | 46／0／3（集合未登記） |
| existing_suites_rust | EVIDENCE_MISSING | 4／0；func-test 3/3 |
| l15_ab | FAIL | 33／2（只剩 `s21-event-latency` 兩邊） |
| event_latency | EVIDENCE_MISSING | Rust 18 輪全 PASS、L1 gate passed；僅缺重新凍結 |
| wal_s1w_perf | FAIL | append p50 0.028／p95 0.041／p99 0.068 ms、rotate max 378 ms（cloud virtio） |
| case_uptake | EVIDENCE_MISSING | case_verdicts 未彙整、OD-02 未填 |
| pytest_full | EVIDENCE_MISSING | 5454／13／226；13 failed 皆屬起點基準（24 failed）子集 |
| policy_check | PASS | fail 0 |

其餘：identity 缺 `daemon_version`（F139）；parity PASS 57／INCOMPLETE 46／BLOCKED-OWNER 2。

event_latency 三輪實測（OWNER-BATCH 用；`p5-evidence/event-latency-od45-rust-evlat-r5-c4cfb96.json`，artifact＝gate `r5b-261029`＠`464adec5`、量測工具 `c4cfb96`，安靜 host）：
| 輪 | 負載 | 事件 | n | max ms | p99 ms | 上限 ms | 判定 |
|---|---|---|---:|---:|---:|---:|---|
| 1 | idle | stop_request | 500 | 0.66 | 0.24 | 50 | PASS |
| 1 | idle | command_cancel | 500 | 2.66 | 0.62 | 50 | PASS |
| 1 | idle | release_request | 500 | 22.34 | 5.69 | 50 | PASS |
| 1 | flood | stop_request | 500 | 2.45 | 0.35 | 100 | PASS |
| 1 | flood | command_cancel | 500 | 1.94 | 0.61 | 100 | PASS |
| 1 | flood | release_request | 500 | 6.35 | 4.21 | 100 | PASS |
| 2 | idle | stop_request | 500 | 3.74 | 0.39 | 50 | PASS |
| 2 | idle | command_cancel | 500 | 2.43 | 0.81 | 50 | PASS |
| 2 | idle | release_request | 500 | 7.43 | 6.00 | 50 | PASS |
| 2 | flood | stop_request | 500 | 7.80 | 0.86 | 100 | PASS |
| 2 | flood | command_cancel | 500 | 2.70 | 0.91 | 100 | PASS |
| 2 | flood | release_request | 500 | 9.95 | 5.33 | 100 | PASS |
| 3 | idle | stop_request | 500 | 2.29 | 0.28 | 50 | PASS |
| 3 | idle | command_cancel | 500 | 3.59 | 1.64 | 50 | PASS |
| 3 | idle | release_request | 500 | 11.76 | 6.18 | 50 | PASS |
| 3 | flood | stop_request | 500 | 1.65 | 0.48 | 100 | PASS |
| 3 | flood | command_cancel | 500 | 15.62 | 1.64 | 100 | PASS |
| 3 | flood | release_request | 500 | 19.29 | 5.70 | 100 | PASS |

L1 結構 gate（`l1_gate`）：`release_handled_within_one_data_item_under_flood`、`shutdown_and_drain_receipt_handled_within_one_data_item_under_flood` 皆 passed。

## 4. 不成立主張

無。

## 5. SPEC-GAPS

- `s21-event-latency` L1.5 情境：OD-45 下兩個 daemon 皆無計時欄位而恆 INSUFFICIENT；且 stop_request 每樣本須重啟 daemon，單一 bridge 情境無法取得 500 樣本。移出 l15_ab 需擁有者逐名裁決（比照 OD-40／46），本輪未自行移出（見 OWNER-BATCH #1）。

## 6. 既有碼待辦（不修）

- `sw-core session_manager::attached_real_paths`：無任何呼叫端（含測試），死碼。
- `wal_health_fanout`、`tx_writer::{try_with, ledger_gauge}`、`uart_bridge::idle_s`、`arbiter::{set_in_flight, physical_len}`、`wal::set_warn_sink` 等測試觀測點未加 `test-fakes` 標註（不影響產品行為）。
- repo 的 Rust 原始碼整體未 rustfmt 乾淨（`cargo fmt --check` 對既有檔大量 diff）；本輪新增程式依周邊風格撰寫。

## 7. OWNER-BATCH

1. **`s21-event-latency` 自 l15_ab 逐名移出**（建議核准）：理由見 SPEC-GAPS；改由 `event_latency` section（L1 結構 gate＋外部量測工具）承擔，情境檔保留。
2. **event_latency 重新凍結（§5.3）**：需獨立審閱者（≠ owner）審 method_revision `2026-10-29.1` 並附 `reviewed_by／reviewed_at／review_conclusion`。數值建議：
   - Rust 在安靜 host 上三輪（gate `r5b-261029` 內一次、`evlat-r5-2472f78`、`evlat-r5-c4cfb96` 各一次，共 54 輪）皆在沿用上限內（release 最大 38.36 ms）；與全套 pytest 並行時曾出現 idle release_request 157／212 ms、idle stop_request 61.5 ms（p99 ≤ 24 ms）。
   - Python 基線（診斷 50 樣本）idle release_request 三輪皆 153～155 ms，超過 50 ms：新口徑含 release 回應前的排空，Python 自身也達不到。
   - 建議：stop_request／command_cancel 維持 50／100 ms；release_request 上限是否放寬（例如 idle 250／flood 300 ms，涵蓋 Python 基線與負載離群值）由擁有者決定。數值變更屬擁有者權限，本輪未改。
3. **gate 集合登記（F50／F57）**：`s21_gate:{linux_cap_posix_pty, blackbox_python, blackbox_rust, existing_suites_rust, pytest_full}` 未在 `l1_required.lock.json`、`s21_gate:py_parity` selector 只含單一 nodeid，使這些 section 恆 EVIDENCE_MISSING。建議：以本輪 `r5b-261029` 的實際收集結果 add-only 登記（blackbox 兩邊 0 failed；pytest_full 13 failed 為環境／鎖類，需先處理 #4 與 #5 或以排除清單登記）。
4. **root 下 L1 收集規則（F108）**：建議 gate 的 linux_l1 由非 root 執行（或 euid==0 時標證據不足並要求非 root 補跑），本輪 7 支 root 失敗非 root 全綠。
5. **`test_l1_judge.py` 3 支、`test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence`**：起點即失敗的鎖／證據類測試，需擁有者處理鎖或證據登記。
6. **`SERVER_STOP_BUDGET` 10→20 s（F100）**：需在 parity-amendments 登記計畫 06 `:4457` 更正。
7. **wal_s1w_perf**：cloud virtio 恆未達 §1.2 Linux 門檻，需擁有者實機量測（建議追加 BO 項）。
8. **OD-02 `stage2_estimate_weeks`** 與 `case_verdicts.json` 彙整規則。

## 8. LOCAL-WINDOWS-TODO

- `windows_native_l1`（`scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_`）；本輪新增的 5 個 nodeid 尚未登記 `s21_core.windows`（需原生 Windows 收集）。
- BO-S21-13 Windows 黑箱／L1.5、Winsock 慢讀、state.json CRLF、`configure_port`、overlapped 讀取失敗臂（F09）、`win_l1_into` 綁定 run（F122）。

## 9. BLOCKED-OWNER

- BO-S21-01～16（實機／Windows）；F50／F57、F108、F100；event_latency 重新凍結；OD-02。

## 10. 距 Slice 2.1 出口還缺什麼

1. 擁有者批次：OWNER-BATCH #1～#4（`s21-event-latency` 移出、event_latency 重新凍結、gate 集合登記、root 收集規則）——缺任一，gate 結構上不可能 READY。
2. P5 第六輪：P6 SPEC-GAP #4（非 root holder 掃描 fail-closed）照修；OD-55（`s21_core` ignore 能力列移集合）。
3. F139：gate identity 取 `daemon_version`。
4. Windows 原生 L1／黑箱（BO-S21-13）與 BO 實機項。
5. slice 出口整包審查（Codex sol＋Claude Opus 雙審）。
