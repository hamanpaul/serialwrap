## A 前輪 finding 核對

本輪未修改前輪已修項目的 production code；下表「已修」表示修正仍保留，不代表重新取得原生平台實跑證據。

| 編號 | 原嚴重度 | 狀態 | 累計未修輪數 | 說明 |
|---|---|---|---:|---|
| 1 | Medium | 已修 `f150a24` | 0 | checked deadline、RAII 回收與逐 job panic 收斂保留。 |
| 2 | Medium | 已修 `f150a24` | 0 | 正式路徑仍使用 `start_fallible`。 |
| 3 | Medium | 已修 `f150a24` | 0 | capture 分類仍依 `foreground_busy()`。 |
| 4 | Medium | 已修 `f150a24` | 0 | 首次回應前已安裝 `log_capture`。 |
| P7-71fe017-1 | Medium | 已修 `030d294` | 0 | 溢位分支仍保留 Child、管線及 stdin 至子行程結束。 |
| P7-71fe017-2 | High | 已修 `7e2da7a` | 0 | `boot_done_regex` 清 quiet、不清 reconfirm pending。 |
| P7-71fe017-3 | High | 已修 `8134806` | 0 | P5 修正已合入，正式命令路徑保留折行回顯剝除。 |
| P7-71fe017-4 | Medium | 已修 `afebff0` | 0 | health 相容掃描與 rollback 保守掃描仍分開；`0baf6e8` 回歸保留。 |
| P7-27b5bf4-1 | High | 已修 `20c9454` | 0 | capture 頂層與巢狀計數仍共用快照。 |
| P7-030d294-1 | High | 已修 `1677489` | 0 | 完成事件仍先核對現行 run 的 `(epoch, fsm_gen)`。 |
| P7-9065f14-1 | Medium | 未修 | 4 | evidence map 仍多出 `l15-delayed-reply`。修正回覆的 NEEDS-RULING 尚非豁免裁決。 |
| P7-c5f0fc9-1 | High | 已修 `7bb9cfd` | 0 | 列名固定對應必要集合並核對 selector。 |
| P7-c5f0fc9-2 | High | 已修 `7bb9cfd` | 0 | 空證據及缺 hash 仍不得 PASS。 |
| P7-c5f0fc9-3 | Medium | 已修 `f9e90a9` | 0 | verify 已要求 CLI、daemon 兩個必要 artifact。 |
| P7-c5f0fc9-4 | High | 已修 `7bb9cfd` | 0 | 整份 JUnit 的 failure／error 與異常退出碼仍阻擋。 |
| P7-c5f0fc9-5 | High | 已修 `f9e90a9` | 0 | 案例必要節點改取凍結集合；只有 `[shell]` 不再通過。 |
| P7-c5f0fc9-6 | Medium | 已修 `29aaa7b` | 0 | 預設輸出父目錄建立保留。 |
| P7-7bb9cfd-1 | Medium | 未修 | 2 | 外部 run 的表頭變成 `run_dir: $RUN_DIR/.`，證據仍只有 `$RUN_DIR/...`，缺少定位基準。應讓表頭記錄可還原的外部基準路徑。 |
| P7-cd7acf0-1 | High | 已修 `f9e90a9` | 0 | 回歸階段與 coverage 已逐項核對凍結節點、缺漏及重複。 |
| P7-cd7acf0-2 | High | 已修 `f9e90a9` | 0 | COM wire／WAL 已直接比較 bytes。 |
| P7-cd7acf0-3 | High | 已修 `f9e90a9` | 0 | COM 案例取樣點已移至送出 banner 前。 |
| P7-cd7acf0-4 | Medium | 未修 | 1 | `61edbd8` 補六個 Linux 集合，但 `p7_gate:contract-blackbox-slice22-b` 仍缺；必要集合登記尚未完整。 |
| P7-cd7acf0-5 | Medium | 已修 `f9e90a9` | 0 | 兩列正式 argv 的 `--dry-run` 解析均回 0。 |
| P7-cd7acf0-6 | Medium | 已修 `f9e90a9` | 0 | mutex guard 涵蓋整案生命週期。 |

## B 新發現

**P7-f9e90a9-1 [High]** [tests/acceptance/l1_required.lock.json:756](tests/acceptance/l1_required.lock.json:756) — 「全套 pytest」必要集合只凍結 49 個節點，會接受不完整證據。

獨立重播：使用實際鎖中的 49 個 nodeid，建立 selector 為 `-q tests/`、49 列 passed、finished、exit 0 的合法 L1 報告；完全沒有 WAL 等其餘測試結果，`_test_row_status()` 仍回 `PASS, …49 筆`。預期應為證據不足。

確切修法：由正式 `-q tests/` selector 完整收集並 add-only 登記必要集合，加入「只提供這 49 列不得 PASS」的反例。

**P7-f9e90a9-2 [Medium]** [tests/test_p7_ab_report.py:54](tests/test_p7_ab_report.py:54) — 新增參數改變六個已凍結 nodeid，正式 gate 永遠缺測。

例如鎖要求 `test_regression_phase_status[rows0-0-PASS]`，目前 pytest 產生的是 `[rows0-0-frozen0-PASS]`；其餘五個舊名稱同樣消失。以新版節點全 passed 重播，L1 judge 回 `INSUFFICIENT`、缺六項。

確切修法：以明確 `ids` 保留原六個 nodeid，再為新增反例配置新 ID 並 add-only 入鎖；不得直接刪除舊必要節點。

**P7-f9e90a9-3 [High]** [func-test/scenarios/s23-quiet-mid-login-zero-tx.yaml:49](func-test/scenarios/s23-quiet-mid-login-zero-tx.yaml:49) — 新 quiet 案例再次漏掉 banner 後的 TX。

具體反例：daemon 收到 banner 後立即送出一個 `\n` 並記入 WAL，之後零 TX。案例等待 banner、睡一秒，才建立 quiet 取樣窗口；該違規 byte 已被排除。以此紀錄重播，`check_zero_tx()` 與帳號連續性檢查均 PASS；wire／WAL 一致也無法拒絕它。

確切修法：讓 DUT 收到帳號後先停住，在送出 banner **之前**建立窗口，再主動 emit banner；補入「banner 後立即送一 byte」必紅的反例。

**P7-f9e90a9-4 [High]** [func-test/scenarios/s23-interleave-login-agent-human.yaml:77](func-test/scenarios/s23-interleave-login-agent-human.yaml:77) — interleave oracle 未涵蓋完整登入序列。

具體反例：第二次登入 wire 為 `root\ns3cretP7\nHUMAN_P7\necho AGENT_P7\necho __READY__2\n`。human／agent 已夾在 secret 與 READY nonce 之間，但案例只要求兩段 `root\ns3cretP7\n`；直接重播 `check_contiguous()` 回 PASS。WAL 同樣記錄這些 bytes 時，比較器也無法識別違規。

確切修法：核對包含 probe／nonce 的完整登入區間，要求 human bytes 不得夾入，agent bytes 僅在 READY 後出現；加入上述 wire 反例。

**P7-f9e90a9-5 [High]** [func-test/scenarios/s23-recover-force-handoff.yaml:43](func-test/scenarios/s23-recover-force-handoff.yaml:43) — handoff 案例沒有驗證關閉、回條或重開。

具體反例：初始為 `ATTACHED＋CREDENTIALS_UNRESOLVED`；錯誤實作收到 force recover 後完全不關閉、不重開，只回覆預期的 `FORCE_RECOVER_TIMEOUT` JSON。狀態維持原值且零 TX，現有全部斷言仍成立。此案例無法驗證計畫要求的「回條後才重開」。

確切修法：加入可追溯的舊 handle 關閉、drain 回條與新 handle 開啟順序證據，要求實際發生一次交接；no-op 與回條前重開兩種錯誤實作都必須轉紅。

驗證：兩支報告器的 38 個單元案例以記憶體檔案介面執行通過，另重播上述判定反例。契約檢查、cfg 邊界、shell 語法通過；policy check 為 25 pass／0 fail／2 warn。`git diff --check` 報告 `tests/test_p7_gate_report.py:263` 尾端空白行。未跑全套 pytest、Rust 或原生 Windows；未修改檔案，HEAD 仍為 `f9e90a9`，工作樹乾淨。

## 既有碼待辦（不計入結論）

無。

## 對裁決的異議（不計入結論）

無。

結論：需修正

