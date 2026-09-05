---
type: fix
issue: 189
scope: wal
---
WAL 檔案不存在時不再靜默回 `ok:true` ＋ 空陣列；daemon 會自癒重建被刪掉的 WAL 目錄。

- **事故**：daemon 同一 PID 連續運行六天，`log tail-raw` / `log tail-text` /
  `wal export` 全部回 `ok:true` ＋ 空陣列 ＋ rc=0，而 `current_seq` 已累加到
  1,261,000——WAL 目錄被外部工具 rmtree 掉了（testpilot 的 `clean_wal()`，已於
  hamanpaul/testpilot-core#36 追蹤），服務對此毫無所覺、也不告訴任何人。該 bench
  六天的 console 紀錄因此無法回溯，兩輪事故取證落空。`doctor` 的 `wal_dir` 檢查
  當時回的是 ok:true——它只比對 shell/daemon 的 WAL_DIR 是否一致並印出路徑，
  **從不檢查該路徑是否存在**。
- **機制修正**：issue 原文推測是「daemon 對著已 unlink 的 fd 續寫」。實際不是——
  `append()` 每筆都重新 `open(path, "a")`，被刪掉的是**整個目錄**，於是每次 append
  都以 `FileNotFoundError` 失敗、被既有的 `except OSError` best-effort 分支吞掉
  （#79 STA-1 的「稽核寫入失敗不得讓 RX thread 崩潰」），而 `_seq` 照常累加。
  告警確實有發，但走 `logging` 而沒有任何 log 落地（正是 #171 的論點）。
- **自癒**：`append()` 撞到 `OSError` 時先 `os.makedirs(wal_dir, exist_ok=True)`
  並重試一次；成功即續寫並告警（記 `recreated_count`），重試仍失敗才維持既有的
  best-effort 標 loss 行為（記 `write_failures` / `last_write_error`）。因為每筆
  append 都重開檔，目錄被刪之後**下一筆寫入**就會偵測到並修好，不需要另開週期性
  自檢執行緒。
- **讀取路徑誠實化**：`WalWriter.health()` 攤開 `wal_dir` / `wal_path` /
  `wal_dir_exists` / `wal_file_exists` / `wal_dir_writable` / `current_seq` /
  `write_failures` / `last_write_error` / `recreated_count` / `healthy`。
  `log.tail_raw` / `log.tail_text` / `wal.range`（＝`serialwrap wal export`）在
  「`current_seq > 0` 但現行檔不存在」時回 `ok:false` ＋
  `error_code: "WAL_MISSING"` ＋ 實際 `wal_path` ＋ 可行動 hint。
  **`current_seq == 0` 不誤報**——全新 daemon 尚無 UART 流量時檔案本來就還沒建立。
  成功路徑也一律帶 `wal_path` / `wal_file_exists`，讓呼叫端分辨「查得到但沒有符合
  的紀錄」與「稽核檔案不見了」。
- **輪替可分辨**：`wal.range` 另回 `available_from_seq`（現行檔最小 seq）與
  `rotated_out`（請求區間是否落在已輪替掉的範圍），使「這個區間本來就沒有紀錄」與
  「曾經存在但已被 rotate 掉」不再都只是空陣列。
- **`doctor` 新增 `wal_writable` 檢查**（消費 `health.status` 新增的 `wal` 欄位）：
  實際驗證目錄存在且可寫，**非 advisory**——稽核紀錄整個消失必須拉低 doctor 整體
  ok。既有 `wal_dir`（shell/daemon 一致性）維持 advisory WARN，兩者各司其職。
  `tests/test_doctor.py` 的兩份 pinned 清單同步更新。

**契約變更**：WAL 檔案缺失時三條讀取路徑由 `ok:true` 改為 `ok:false` ＋
`WAL_MISSING`。這是本 issue 的核心訴求（先前的靜默成功正是讓事故延續六天的原因）。
只讀 `records` / `lines` 的呼叫端行為不變（仍是空的）；檢查 `ok` 的呼叫端會開始看到
失敗——那正是期望的行為。

**regression-case 評估**：新增 `tests/test_wal_missing_detection.py`（19 個 pytest，
修復前 18 個可重現地 FAIL）——涵蓋 health 欄位、rmtree 後自癒重建並告警、重建後可再
讀、重建失敗時標 loss 且不崩、`available_from_seq`、三條讀取路徑回 `WAL_MISSING`、
全新 daemon 不誤報、成功路徑帶 `wal_path`/`wal_file_exists`、`rotated_out` 判定、
`health.status` 暴露 wal 健康，以及 doctor `wal_writable` 的四種情形與非 advisory
性質。全部可用 tmpdir ＋ `shutil.rmtree` 精確模擬，**不需**新增 `regression/`
（TestPilot 實機）case——本修復不依賴真板時序或外部工具。
