# Linux 24h lr-mixed 長跑：延遲、吞吐、錯誤與資源（0.4.1 基線 vs 0.5.0-rc.1 候選）

- 產出：`latency-linux.json`（全部數字）、`tools/latency.py`（計算腳本）、`SHA256SUMS`
- 重跑：`python3 -I tools/latency.py <基線目錄> <候選目錄> latency-linux.json`
- 只讀以下兩個 run 目錄的既有檔案，沒有動板子、service，也沒有跑新的長跑。各輸入檔的 sha256 記在 JSON 的 `runs.*.inputs_sha256`。
  - 兩個目錄都在測試機的 b-log 底下，這裡只寫目錄名稱。
  - 基線：`sw-longrun-baseline-linux-20261120`（0.4.1，Python daemon；2026-11-20T10:27:05Z 起）
  - 候選：`sw-longrun-cand-linux-20261121`（0.5.0-rc.1，Rust daemon；2026-11-21T10:52:40Z 起）
- 兩次的 harness 程式完全相同（`longrun.py`、`drivers.py`、`harness.py`、`procobs.py`、`frontends.py` 逐檔 diff 無差異）。兩版的 CLI 都是原生 ELF，但屬於不同 build；sha256 與各自 `report.json` 的 `cli_identity.sha256` 相符。

## 方法

- 時間一律取 `wall_ts`（微秒精度），不用整數秒的 `t`。
- 配對鍵採 `counting.pair_key = (source, round)`；background 的 `submit_accepted` → `bg_result` 以 `cmd_id` 接續。腳本會檢查每個鍵是否剛好一筆 submit 和一個終態；兩次都沒有重複、缺漏或負值。
- 分位數用 nearest-rank，不插值。每格都附 `n`；`n < 1000` 時 p99.9 等於最大值附近的單一樣本，JSON 中以 `n_below_p99_9_resolution: true` 標示。
- 延遲分位數只算成功完成的命令；非預期錯誤各自的延遲列在 `unexpected.items`。
- 分組：全體（`*`）、每個 `COM|worker`，再依 mode 分成 line、interactive、background。`all_modes` 是三種 mode 混合的 submit → 完成，只供參考。

### 各 mode 實際可算的區間

| mode | 事件序列 | 可算的延遲 |
|---|---|---|
| line | submit → done／error | `submit_to_done_s`。**這個 mode 沒有 `submit_accepted`**，所以 submit_accepted → done 為「沒有」 |
| interactive | submit → done／busy／error | `submit_to_done_s`（interactive-open 加 close）；被拒者另列 `submit_to_reject_s` |
| background | submit → submit_accepted → bg_result | `submit_to_accepted_s`、`accepted_to_bg_result_wall_s`、`bg_result_latency_s_field`（事件內的 `latency_s`）、`submit_to_bg_result_s`。**background 沒有 `done` 事件**，以 `bg_result.status == done` 為完成，與 acceptance 的 `completed` 定義相同 |

### 解讀限制（重要）

1. **line 延遲有 harness 設下的下限**：`submit_and_wait` 送出後先固定睡 1.5 s，之後每 0.5 s 輪詢一次 `cmd status`。所以 line 的最小值約 1.50 s，分佈以 0.5 s 為階。這段量的是 harness 看到結果的時間，不是 daemon 的命令執行時間。
2. **submit 事件在取得 per-board lock 之前寫入**：同板的其他 worker 若正持有 lock，等待時間會算進延遲裡。事件裡沒有取得 lock 的時間戳，所以等 lock 與執行無法分開，這部分為「沒有」。
3. **`bg_result` 的時間是 resolver 輪詢到的時刻**：resolver 每輪睡 1 s，並依序對所有 pending 送 `cmd status`。所以 accepted → bg_result 是上限值，解析度約 1 s；p50 約 0.5 s、p99 約 1.0 s，與 1 s 輪詢一致。事件內的 `latency_s` 與 wall 差值的最大差距是 0.008 s（基線）和 0.010 s（候選）。
4. 所有數字都是端到端，包含 CLI 子程序啟動與 RPC 來回。不受 harness 輪詢下限影響的只有 interactive 和 background 的 submit→accepted 兩項。
5. **吞吐是閉迴路負載**：每個 worker 每輪都要等前一個動作結束，line 另有 1.5 s settle，每輪之間再隨機睡 0.5–2.0 s。所以完成數只能說明「沒有變慢」，不是容量量測。
6. `effective_hour` 直接取自各 run 的 `release-gate.json` `counts`，兩板全程 READY，因此等於全長。基線 report 的 `duration_s` 是 86405.13 s，events 的 start → stop 是 86403.74 s；候選分別是 86403.64 s 和 86402.44 s。

## 交叉驗證（全部吻合）

- 每個 `COM|worker` 的完成數、預期拒絕數和非預期錯誤數，都與該 run 的 `release-gate.json` `counts` 相同（基線 124606／2398／19，候選 125525／1876／10）。
- 候選 RSS 重現 gate 的 `first 14224／peak 22428／steady 22032／slope 55.171`；基線重現 `od09-stats-linux.json` 的 `peak 32312／steady 32260／slope −9.526`。RSS 用 gate 的 `_stats_resources` 定義：steady 是 `t ≥ duration_s − 86400` 樣本的中位數，slope 是同一窗的最小平方法斜率。

## 主要數字（基線 0.4.1 → 候選 0.5.0-rc.1，單位秒）

| 指標 | n | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|---|
| line submit→done | 76201 → 76441 | 1.711 → 1.699 | 3.212 → 3.194 | 4.401 → 4.340 | 8.201 → 5.073 | 14.058 → 13.018 |
| interactive submit→done | 10303 → 10864 | 0.112 → 0.163 | 1.694 → 1.605 | 2.836 → 2.774 | 3.469 → 3.748 | 10.533 → 10.782 |
| background submit→accepted | 38106 → 38225 | 0.009 → 0.004 | 1.434 → 1.440 | 2.627 → 2.601 | 3.638 → 3.277 | 11.397 → 9.204 |
| bg_result `latency_s`（accepted→結果） | 38102 → 38220 | 0.557 → 0.511 | 0.956 → 0.912 | 1.050 → 1.002 | 1.073 → 1.018 | 11.100 → 10.463 |
| background submit→bg_result | 38102 → 38220 | 0.857 → 0.805 | 2.069 → 2.027 | 3.277 → 3.185 | 4.580 → 4.050 | 12.023 → 10.466 |

- 不受輪詢下限影響的兩項，方向不同。
  - background submit→accepted 的 p50 從 9 ms 降到 4 ms。
  - interactive submit→done 的最小值從 0.0038 s 升到 0.0257 s，p50 從 0.112 s 升到 0.163 s，四個有 interactive 的格子中有三格 p50 上升；被拒的最小值也從 0.0021 s 升到 0.0133 s。
  - 原因從事件檔無法判定，為「沒有」。
- 長尾只出現在 COM0（有 human minicom 的那塊板）。line 延遲超過 6 s 的筆數：基線 COM0 84 筆，其中 27 筆與同板非預期錯誤的 submit→終態區間重疊；候選 COM0 22 筆，其中 7 筆重疊。COM1 兩次都是 0 筆。其餘的原因從事件檔無法判定。
- 吞吐：全體完成 124606 → 125525，每小時 5191.7 → 5230.1（+0.74%）。各 `COM|worker` 與每小時分桶的最小／中位數／最大值見 JSON 的 `throughput`、`hourly_completed`。
- 非預期錯誤 19 → 10，全部在 COM0。
  - 基線：FLUSHED_BY_RECOVERY 15 筆（line 11、background 4）；status=done 但 stdout 沒有 marker 4 筆。
  - 候選：FLUSHED_BY_RECOVERY 9 筆（line 4、background 5）；status=done 但 stdout 沒有 marker 1 筆。
  - 逐筆清單見 `unexpected.items`。
- 預期拒絕 2398 → 1876。
  - `busy`（SESSION_INTERACTIVE_BUSY）1699 → 1740，只在 COM0。
  - interactive `SESSION_BUSY` 699 → 136。基線 COM0 361、COM1 338；候選 COM0 135、COM1 1。
- RSS（kB）：首值 30372 → 14224，峰值 32312 → 22428，穩態中位數 32260 → 22032，斜率 −9.53 → +55.17 kB/h。
- observer：兩次都只有 daemon 健康與 session 狀態欄位，**CPU、thread、fd 為「沒有」**。兩次各 8648 筆樣本，daemon 都正常回應、WAL 都健康，沒有 multi_open，非 READY 0 筆，pid 全程沒變。
- 補充（只有候選有，不能拿來比較）：`proc-detail.ndjson` 是本機另外記錄的 /proc 資料，從 2026-11-21T11:55:49Z 到 2026-11-22T10:46:10Z，每 600 s 一筆，共 138 筆。fd 27–29，threads 33–34，VmRSS 21088–22428 kB。CPU 為「沒有」。基線為「沒有」。
