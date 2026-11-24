# Windows lr-mixed 長跑：延遲、服務時間、吞吐、錯誤與資源（0.4.1 基線 vs 0.5.0-rc.1 候選）

- 產出：`latency-windows.json`（全部數字）、`tools/latency_win.py`（計算腳本）、`SHA256SUMS`
- 重跑（在本目錄執行；兩次輸出逐 byte 相同）：

  ```bash
  python3 -I tools/latency_win.py \
    --baseline <基線目錄> --baseline-name sw-longrun-baseline-win-20261120 \
    --candidate <候選目錄> --candidate-name sw-longrun-cand-win-20261121 \
    --cutoff 2026-11-22T05:29:16+00:00 \
    --od110-gate ../od110/release-gate-od110.json \
    --od110-receipt ../od110/truncation-receipt.json \
    --out latency-windows.json
  ```

- 只讀以下兩個 run 目錄的既有檔案，沒有動板子、daemon，也沒有跑新的長跑。各輸入檔的 sha256 記在 JSON 的 `runs.*.inputs_sha256`。兩個目錄都在測試機上，這裡只寫目錄名稱。
  - 基線：`sw-longrun-baseline-win-20261120`（0.4.1，Python daemon；2026-11-20T00:10:53Z 起）。讀的是測試機上另存的副本；events、snapshots、observer、report.json 與三個 WAL 檔的 sha256 都與 `evidence/stage0/baseline/windows/release-gate.json` 的 `evidence_hashes` 相同（`inputs_match_gate_evidence_hashes` 全為 true）。
  - 候選：`sw-longrun-cand-win-20261121`（0.5.0-rc.1，Rust daemon；2026-11-21T10:49:33Z 起）。只讀。sha256 與 `evidence/stage2/longrun/windows/release-gate.json` 的 `evidence_hashes` 相同，也與 `../od110/truncation-receipt.json` 的 `src_sha256` 相同（`od110_receipt_src_sha256_match`）。
- 兩次的負載產生器相同：`realhw/cases/longrun.py`、`drivers.py`、`harness.py`、`procobs.py`、`frontends.py` 逐檔 sha256 相同（`engine_files_identical` 全為 true；雜湊在 `runs.*.engine_sha256`）。兩版的 CLI 都是原生 `.exe`，但屬於不同 build（sha256 見兩份判定檔的 `run.cli_identity.sha256`）。
- 硬體：Orange Pi 3 一塊板（`COM0`），4 個 agent worker（`rhw0`～`rhw3`）、1 個 human console、1 個 background 結果輪詢器。

## 區段

| JSON 路徑 | 範圍 | 長度 |
|---|---|---|
| `runs.baseline_python_0.4.1.segments.full` | start → stop 事件 | 86400.789 s |
| `runs.candidate_rust_0.5.0_rc1.segments.effective_od110` | start → OD-110 截斷點 2026-11-22T05:29:16Z | 67182.824 s |
| `runs.candidate_rust_0.5.0_rc1.segments.full` | start → stop 事件，包含 05:29:17Z 起 session 停在 DETACHED 的區間 | 68124.377 s |

- 有效段的切法與 `../od110/truncate.py` 相同：只計 `wall_ts` 早於截斷點的命令層事件、快照與 observer 樣本。OD-110 的背景見 `../../README.md`。
- 比較一律用「基線全程 vs 候選有效段」，衍生數字放在 `comparison_effective_vs_baseline`。候選全程只供對照。

## 方法

- 時間一律取 `wall_ts`，不用整數秒的 `t`。events 與 WAL 的 `wall_ts` 都寫到微秒，相鄰紀錄的最小正差值都小於 0.0004 s（`runs.*.timestamp_resolution`）。
- 配對鍵採 `(source, round)`；background 的 `submit_accepted` → `bg_result` 以 `cmd_id` 接續。腳本會檢查每個鍵是否剛好一筆 submit 和一個終態，三個區段都沒有重複或缺漏（`anomalies` 為空），也沒有負值（`negative_deltas` 為空）。
- 分位數用 nearest-rank，不插值。每格都附 `n`；`n < 1000` 時以 `n_below_p99_9_resolution: true` 標示。
- 延遲分位數只算成功完成的命令。預期拒絕（`busy`、interactive 的 `SESSION_BUSY`）另列 `submit_to_reject_s`；非預期錯誤列在 `unexpected`。
- 分組：全體（`*`）與每個 `COM0|agent:rhwN`，再依 mode 分成 line、interactive、background；`all_modes` 是三種 mode 混合，只供參考。

| mode | 事件序列 | 可算的延遲 |
|---|---|---|
| line | submit → done／error | `submit_to_done_s`。這個 mode 沒有 `submit_accepted` |
| interactive | submit → done／busy／error | `submit_to_done_s`（interactive-open 加 close）；被拒者另列 `submit_to_reject_s` |
| background | submit → submit_accepted → bg_result | `submit_to_accepted_s`、`accepted_to_bg_result_wall_s`、`bg_result_latency_s_field`（事件內的 `latency_s`）、`submit_to_bg_result_s`。以 `bg_result.status == done` 為完成，與判定器的 `completed` 定義相同 |

### 板鎖排隊與服務時間（估計）

- harness 對同一塊板的所有 agent 動作共用一把 per-board lock。submit 事件在取得鎖之前寫入；終態事件（background 為 `submit_accepted`）在釋放鎖之後立刻寫入（`realhw/cases/longrun.py` 的 `_do_line`／`_do_background`／`_do_interactive`）。
- 事件裡沒有取得鎖的時間戳，所以用估計：同一塊板的動作依事件檔順序排列，推定取得鎖時間 `acq = max(本動作 submit, 前一動作終態)`。
  - `lock_wait_est_s = acq − submit`，`service_time_est_s = 終態 − acq`。
  - `lock_busy_fraction_est` 是服務時間總和除以區段長度。
- 這個估計依賴「同一塊板的動作都經過同一把鎖」，腳本依 `com` 分開計算。這次 Windows 只有 `COM0`，四個 worker 的所有動作都在同一條鏈上。
- 前一動作的終態寫在釋放鎖之後，所以 `acq` 會略晚於實際取得鎖的時刻，服務時間略為低估。偏差多大沒有量測，為「沒有」。
- line 的服務時間包含 harness 固定的 1.5 s settle；`line|done_minus_settle_1.5s` 是扣掉 1.5 s 後的值，主要是 `cmd submit` 與第一次 `cmd status` 兩個 CLI 子程序的來回。

### WAL 拆解

- events 與 daemon 的 WAL 寫在同一台機器、同一個系統牆鐘，所以可以用 `wall_ts` 跨檔相減。
- 對每筆成功的 line 命令，在 WAL 找同一個 marker 的 agent TX，接著依 `seq` 往後串接 RX：
  - marker 第 2 次出現是輸出行，第 1 次是 shell 回顯的命令行。
  - 輸出行之後第一個 prompt 結尾 `$ ` 視為命令結束。
- 算出三段：`lock_acq_to_wal_tx_s`（推定取得鎖 → WAL TX）、`wal_tx_to_output_rx_s`、`wal_tx_to_prompt_rx_s`。background 只算到 WAL TX；human 算 tick 事件 → WAL TX → 輸出行 → prompt。
- 只輸出時間，不輸出任何 WAL 或 RX 內容。

### 系統牆鐘跳變

- 兩次長跑各有一次系統牆鐘往回跳（`runs.*.clock_steps_observer`）。偵測方式是 observer 相鄰樣本的 `wall_ts − t_mono` 變化超過 0.1 s；WAL 的 `wall_ts − mono_ts_ns` 也在同一處看到同樣幅度（`runs.*.clock_steps_wal`）。
  - 基線：2026-11-20T00:47:17Z～00:47:26Z 之間 −1.541 s。
  - 候選：2026-11-22T00:21:45Z～00:21:54Z 之間 −1.493 s。
- 處理方式：
  - 跳變前後各留 |跳變幅度| 的餘裕，依檔案順序把這段標為模糊區（`segments.*.clock_step_zones`；基線 30 筆事件，候選 25 筆）。
  - 兩端跨過模糊區或落在模糊區內的配對，不計入 wall 相減的延遲與服務時間。排除筆數見 `clock_step_excluded_pairs` 與 `service_chain_excluded`。
    - 基線：line 11 筆、background 4 筆；服務時間鏈排除 15 筆。
    - 候選有效段：line 9 筆、interactive 1 筆、background 4 筆；服務時間鏈排除 14 筆。
  - 完成數、拒絕數、錯誤數不受影響。`bg_result_latency_s_field` 是 harness 的 monotonic 時鐘算的，也不受影響。
- 不排除時，跨過跳變的配對會少算約 1.5 s，服務時間甚至會出現低於 1.5 s settle 的值，這不可能發生。排除後 line 服務時間的最小值是 1.5241 → 1.5312 s（`service_time_est_s.line|done.min`）。

## 解讀限制（重要）

1. **line 延遲有 harness 設下的下限**：`submit_and_wait` 送出後先固定睡 1.5 s，之後每 0.5 s 輪詢一次 `cmd status`。量到的是 harness 看到結果的時間，不是 daemon 的命令執行時間。
2. **原始延遲大半是板鎖排隊**：4 個 worker 共用一把鎖，鎖約 96% 的時間被佔用（`lock_busy_fraction_est` 0.9605 → 0.963）。推定的等鎖時間中位數就有 1.753 → 1.789 s（`lock_wait_est_s.*`），所以 submit→done 的差異主要反映排隊，不是 daemon。Windows 的 line p50（約 3.2 s）比 Linux（約 1.7 s，`../../linux/latency/latency-linux.json`）高，原因應該也在這裡：Linux 有兩塊板，每個 worker 輪流對兩板送命令，同一塊板的排隊較少（推論）。兩平台的原始延遲不能直接比較。
3. **服務時間是 CLI 加 daemon 的合計**：包含 CLI 子程序啟動與 RPC 來回，兩版 CLI 又是不同 build，所以毫秒級的差異不能只歸因給 daemon。從「推定取得鎖」到 WAL 記下 TX（`lock_acq_to_wal_tx_s`）的中位數兩版相同（0.0456 → 0.0457 s）；從 WAL TX 到 prompt 回來的中位數也幾乎相同（0.0231 → 0.0235 s）。
4. **background 的解析度**：
   - `bg_result` 的時間是 resolver 輪詢到的時刻。resolver 每輪依序對所有 pending 送 `cmd status`，之後睡 1 s，所以 accepted → bg_result 是上限值，解析度約 1 s。
   - 事件內的 `latency_s` 由 harness 的 Python `time.monotonic()` 計算，在這台 Windows 上以約 15.6 ms（1/64 s）為一格：兩次的值 100% 落在 1/64 s 整數倍 ±1.5 ms 內（`timestamp_resolution.bg_result_latency_s_share_within_1_5ms_of_1_64s_multiple`；均勻分佈時約 19%）。
   - `latency_s` 與 wall 差值的 p99 是 0.0147 → 0.0146 s，最大值是 0.2011 → 0.5053 s（`bg_latency_field_vs_wall_abs_diff_s`）。
5. **吞吐是閉迴路負載**：每個 worker 每輪都要等前一個動作結束，line 另有 1.5 s settle，每輪之間再隨機睡 0.5–2.0 s。所以完成數只能說明「沒有變慢」，不是容量量測。
   - 候選全體每小時完成數高 0.652%（`completed_per_h_ratio` 1.00652），每小時提交數卻低 0.997%（`submitted_per_h_ratio` 0.99003）。
   - 依 mode 拆開（`comparison_effective_vs_baseline.throughput_by_mode`）：line 與 background 每小時完成數分別是基線的 0.98998 與 0.98999 倍，在閉迴路下約少 1%；只有 interactive 是 1.27141 倍。
   - interactive 變多，是因為被 `SESSION_BUSY` 拒絕的比例從 22.589% 降到 0.317%（`interactive_session_busy_share`）。全體 +0.652% 不能解讀成「整體變快」。
6. **吞吐的分母**是區段的 wall 長度（`span_s`）。這次牆鐘跳變讓 wall 長度少算約 1.5 s（`span_s_clock_step_corrected`），影響小於 0.003%。
   - 判定檔 `counts.*.effective_hour` 另列在 `gate_effective_hour` 供核對。
   - OD-110 判定檔的 18.9236 h 包含已排除的 DETACHED 區段（分母用引擎的 68125 s），所以它的 `throughput_per_h` 比這裡低。這裡的有效段用的是 start → 截斷點的 18.6619 h。
7. **WAL 時戳是 daemon 寫 WAL 的時刻**，不一定是 bytes 上線的時刻。
   - 基線（Python daemon）的 WAL `mono_ts_ns` 解析度只有 0.015 s（`timestamp_resolution.wal_mono_ts_ns_min_consecutive_diff_s`），所以 WAL 內部區間一律用 `wall_ts`。
   - line 的 WAL TX → 輸出行有 8 → 7 筆在 2 ms 內（`output_rx_within_2ms_of_tx`），遠低於分佈的中位數（約 15 ms），研判是時戳假象（推論）。佔比低於 0.02%，不影響分位數。
   - 候選的 WAL 有 7 筆 agent TX 紀錄的 `mono_ts_ns` 比 `wall_ts` 早 0.1535～2.1594 s，下一筆即回復（`wal_single_record_mono_wall_spikes`），不是牆鐘跳變。
     - line 的 `lock_acq_to_wal_tx_s` 最大兩筆（2.1625、1.8186 s）與 background 最大一筆（2.1911 s），WAL TX 都落在這幾筆尖峰的同一秒（`lock_acq_to_wal_tx_top3`）。
     - 這段延遲發生在 CLI 或 daemon 內的哪一處，從這些檔案無法判定，為「沒有」。
8. **候選有一段原因不明的 RX 停頓**：有效段 WAL 最大的相鄰紀錄間隔是 4.261 s，前一筆是 agent TX，下一筆 RX 在 2026-11-21T13:45:29.62Z 才出現（`wal.largest_gaps_wall_s`）。line 延遲最大的兩筆分別在 13:45:22Z 和 13:45:34Z 送出（`line_done_top5`）。原因從這些檔案無法判定，為「沒有」。
9. **RSS 只有 WorkingSetSize**，每 300 s 一筆，由 harness 以 `GetProcessMemoryInfo` 讀取，不是 Linux 的 VmRSS。CPU、thread、handle 都是「沒有」。
   - 判定器定義的 steady／slope，基線用 24 h、候選有效段只有 18.66 h，窗口長度不同。
   - 比較兩版時另看共同窗口 `rss_common_window`：t ∈ [14400 s, 67182.824 s)。基線 WorkingSet 在 t 為 2–3 h、3–4 h、4–5 h 的每小時中位數分別是 38404、36208、30932 kB（`resources.hourly_median_kb` 的鍵 `2`、`3`、`4`），一次性下降後就穩定，所以窗口從 4 h 起算。

## 交叉驗證（全部吻合）

- 每個 `COM0|worker` 的完成數、預期拒絕數、非預期錯誤數，都與對應判定檔 `counts` 相同（`throughput.*` 內的 `gate_*` 欄位）。全體為：
  - 基線：82209／3768／0，對 `evidence/stage0/baseline/windows/release-gate.json`。
  - 候選有效段：64340／1843／2，對 `../od110/release-gate-od110.json`。
  - 候選全程：64342／1843／2862，對 `../release-gate.json`。
- 有效段另有 2 筆在截斷點前送出、截斷點後才完成的命令（`cut_by_segment_end`）：一筆 line，結果 `done`；一筆 background，結果 `bg_result:done`。
  - 判定器把這 2 筆算成 `unresolved`，所以 OD-110 判定檔的錯誤率是 6.2e-05，即（2 筆錯誤＋2 筆 unresolved）／（64340＋2＋2）（`unexpected.gate_error_rate`）。
  - 實際的非預期錯誤只有 2 筆，以「完成＋非預期」為分母是 3.108e-05（`unexpected.rate_vs_completed_plus_unexpected`）。
- RSS 依判定器 `_stats_resources` 的定義重算（steady 取 `t ≥ report.duration_s − 86400` 樣本的中位數，slope 是同一窗的最小平方法斜率），結果與判定檔的 `resources` 相同。
  - 基線：peak 38404／steady 30868／slope −233.106（287 筆）。
  - 候選有效段：35396／33906／31.939（224 筆）。
  - 候選全程：35396／33902／29.698（228 筆）。
- OD-110 判定檔的 `report.json` 與 WAL 雜湊，也與候選原始檔相同（`od110_gate_crosscheck`）。

## 主要數字（基線 0.4.1 全程 → 候選 0.5.0-rc.1 有效段，單位秒）

### 端到端延遲（含排隊與 harness 輪詢）

| 指標 | n | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| line submit→done | 51575 → 39701 | 3.228 → 3.288 | 4.924 → 4.992 | 5.715 → 5.777 | 6.305 → 7.232 |
| interactive submit→done | 4829 → 4773 | 2.076 → 1.897 | 3.681 → 3.534 | 4.256 → 4.298 | 4.691 → 4.828 |
| interactive 預期拒絕 submit→reject | 3768 → 1843 | 1.573 → 1.811 | 3.151 → 3.569 | 4.112 → 4.245 | 4.635 → 6.035 |
| background submit→accepted | 25790 → 19853 | 1.961 → 1.998 | 3.590 → 3.630 | 4.190 → 4.244 | 4.712 → 4.697 |
| background accepted→bg_result（wall） | 25790 → 19852 | 0.575 → 0.572 | 0.976 → 0.974 | 1.092 → 1.086 | 1.194 → 2.938 |
| bg_result `latency_s` | 25794 → 19856 | 0.578 → 0.563 | 0.969 → 0.969 | 1.094 → 1.079 | 1.188 → 2.937 |
| background submit→bg_result | 25790 → 19852 | 2.536 → 2.564 | 4.160 → 4.212 | 4.944 → 4.997 | 5.428 → 5.878 |

### 服務時間（扣掉板鎖排隊的估計）

| 動作 | n | p50 | p90 | p99 | mean |
|---|---|---|---|---|---|
| line done（含 1.5 s settle） | 51575 → 39701 | 1.5772 → 1.5915 | 1.6004 → 1.6150 | 1.6321 → 1.6525 | 1.5792 → 1.5932 |
| line done − 1.5 s | 51575 → 39701 | 0.0772 → 0.0915 | 0.1004 → 0.1150 | 0.1321 → 0.1525 | 0.0792 → 0.0932 |
| interactive open＋close | 4829 → 4773 | 0.0779 → 0.0926 | 0.0956 → 0.1093 | 0.1257 → 0.1424 | 0.0782 → 0.0903 |
| background submit | 25790 → 19853 | 0.0330 → 0.0472 | 0.0486 → 0.0516 | 0.0791 → 0.0953 | 0.0395 → 0.0466 |

- 每個動作，候選的平均服務時間多 7.1～14.0 ms（`comparison_effective_vs_baseline.service_time_est_s.*.mean_diff_ms`）。這是 CLI 加 daemon 的合計（見限制 3）。

### WAL 拆解

| 區間 | n | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| line：推定取得鎖→WAL TX | 51575 → 39701 | 0.046 → 0.046 | 0.051 → 0.050 | 0.078 → 0.093 | 1.283 → 2.163 |
| line：WAL TX→輸出行 RX | 51575 → 39701 | 0.015 → 0.015 | 0.017 → 0.017 | 0.018 → 0.018 | 0.036 → 4.261 |
| line：WAL TX→prompt RX | 51575 → 39701 | 0.023 → 0.024 | 0.025 → 0.025 | 0.026 → 0.026 | 0.045 → 4.261 |
| background：推定取得鎖→WAL TX | 25790 → 19853 | 0.046 → 0.046 | 0.052 → 0.050 | 0.078 → 0.094 | 0.429 → 2.191 |
| human：WAL TX→輸出行 RX | 410 → 319 | 0.013 → 0.013 | 0.014 → 0.015 | 0.015 → 0.016 | 0.021 → 0.016 |

- background 的 `submit_accepted` 事件時戳晚於 WAL TX 時戳的比例從 0.7755 變成 0.9973（`accepted_event_after_wal_tx_share`）。也就是基線約有 22% 的 background 命令，harness 記下 accepted 的時刻早於 daemon 在 WAL 記下 TX 的時刻；候選幾乎都晚於 TX。WAL 時戳不等於 bytes 上線的時刻（限制 7），所以這只能說明兩版回報 accepted 與寫 WAL 的先後不同（推論）。

### 吞吐、錯誤、資源

- 吞吐：全體每小時完成 3425.344 → 3447.667（+0.652%），各 worker 的比值是 1.00476～1.00992（`comparison_effective_vs_baseline.throughput`）。
  - 依 mode：line 2149.397 → 2127.865、background 1074.74 → 1063.986、interactive 201.206 → 255.815（`throughput_by_mode`；見限制 5）。
  - 每小時分桶的最小／中位數／最大值見 `hourly_completed`。
- 預期拒絕：3768 → 1843。
  - `busy`（SESSION_INTERACTIVE_BUSY）1826 → 1822。
  - interactive `SESSION_BUSY` 1942 → 21。
- 非預期錯誤：0 → 2。候選的 2 筆都是 line，`detail=done`（status 為 done，但 stdout 沒有 marker），逐筆見 `unexpected.items`。
- 候選全程另有 2860 筆 `SESSION_NOT_READY`（line 1716、background 858、interactive 286），全部在 05:29:17Z 之後。全程非預期錯誤率因此是 0.04258675。
- RSS（kB，WorkingSetSize）：
  - 首值 37064 → 11816，峰值 38404 → 35396。
  - 共同窗口的中位數 30802 → 33890，斜率 −22.036 → −32.695 kB/h（`rss_common_window`）。
- observer：兩次的有效範圍內 daemon 都正常回應、WAL 都健康，沒有 multi_open，非 READY 0 筆，pid 全程沒變。CPU、thread、handle 為「沒有」。
  - 候選全程有 94 筆 `COM0:DETACHED` 樣本。
- 排他性探測：基線 288 筆、候選有效段 224 筆，全部 `exclusive`。候選全程另有 3 筆 `inconclusive|not_ready`。
