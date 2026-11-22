# Stage 2（0.5.0-rc.1）候選長跑證據

OD-102 規定的 rc.1 候選 24 小時 `lr-mixed` 長跑判定結果。兩平台都以凍結門檻檔 `realhw/acceptance/longrun_acceptance.yaml`（revision `2026-11-21.1`，sha256 `e647d9080b8d19c66e78d9aff3e9d4379a2db6aaa3a2f278704d28389be5efe7`，OD-109）以 `--mode candidate` 判定，基線為同平台的 Python 0.4.1 24 小時基線。

| 平台 | 判定檔 | 結果 | 說明 |
|---|---|---|---|
| Linux（兩塊 BRCM 板，COM0／COM1） | `linux/release-gate.json` | **PASS**（25／25） | 2026-11-21T10:52:40Z 起 86402 s；非預期錯誤 10 筆（7.97e-05，基線 1.53e-04），全在 COM0：recovery flush 9 筆、human 行被拆段時 agent 插入 1 筆；RSS peak 22428 kB／steady 22032 kB／slope 55.2 kB/h |
| Windows（OPI，COM6） | `windows/release-gate.json` | 官方 **FAIL** | 2026-11-22T05:29:17Z USB 重新列舉後 session 停在 DETACHED，引擎在 t=68125 s 以 `both_boards_stuck` 自行停止 |
| Windows（OD-110 重算） | `windows/od110/release-gate-od110.json` | 依 OD-110 接受 | 見下節 |

判定檔內的 `run.cli_identity`／`deployed_content` 記錄了受測 CLI 與 daemon 在測試機上的實際安裝路徑（測試機使用者目錄），不含帳密；檔案須保留原文供雜湊核對，`.project-policy.yml` 的 `secret_scan.allow` 依擁有者 OD-110 放行本目錄。

## Windows 候選與 OD-110

**事件**：2026-11-22T05:29:17Z，CH340（COM6）被 Windows 重新列舉（`DEVPKEY_Device_LastArrivalDate`），擁有者說明是插拔麥克風時動到。daemon 重開序列埠得到 `ATTACH_FAILED:FileNotFoundError`，session 停在 DETACHED（`windows/od110/detached-session-list.json`，事後以唯讀 `session list` 取得），之後所有 agent 命令回 `SESSION_NOT_READY`；15 分鐘後長跑引擎以 `both_boards_stuck` 停止。attach 失敗後不自動重試是 Python 0.4.1 與 Rust rc.1 共有的既有限制（契約 `session_last_errors.yaml` 規定 `ATTACH_FAILED` 不自動重探），列為 0.5.0 已知問題。

**OD-110（擁有者 2026-11-22 裁決）**：排除擾動區間後重算，接受有效的 18.66 小時，不重跑。

- 重算方式：`windows/od110/truncate.py` 由原始證據產生截斷副本——`events.ndjson` 只刪除 `[2026-11-22T05:29:16Z, 05:44:58.842Z]` 內的命令層事件（`submit`、`submit_accepted`、`done`、`error`、`busy`、`bg_result`、`tick`），生命週期事件（start、stop、worker_*、major）保留；`snapshots.ndjson` 與 `observer.ndjson` 截在 05:29:16Z 之前；`report.json`、WAL 與排他性證據不截、不改（WAL 安全掃描仍為全量）。各檔原始與截斷後的 sha256 見 `windows/od110/truncation-receipt.json`。原始證據未改動。
- 重算結果：`error_rate_absolute`（2 筆，6.2e-05）、`min_effective_load`、`throughput_relative`、`stuck_relative`、`comparison_uncertainty`、`baseline_compatible` 與全部安全、liveness 檢查 PASS。
- OD-110 豁免的項目：
  - `engine_verdict`、`continuous_duration`（68125 s＜86400 s）、`worker_liveness`（引擎自行停止）、`observation_gaps` 與 `safety_double_holder`（缺口都在排除區間內）——皆由擾動與其後的引擎停止造成。
  - `resources`：數值 peak 35396 kB、steady 33906 kB、slope 31.9 kB/h 全在門檻（48005／38585／256）內，只因運轉較短而快照數 224＜288。
  - `error_rate_relative`：排除區間外的 2 筆 `detail=done` 經 WAL 逐筆核對皆為 issue #294 B 型（human 整行送出後、其 prompt 回來前 agent 命令送出，完成判定被 human 的 prompt 滿足），6.2e-05 高於基線 0 但落在 0.002 容差帶內；#294 已由擁有者裁定為 0.5.0 已知問題、0.5.1 修。
