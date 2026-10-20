# Windows Task 重啟程序驗證（p1-rst-daemon 監管面）

本文件提供在 Windows Task Scheduler 監管模式下執行 daemon 優雅重啟、故障恢復與程序交接的標準操作程序（對應 P2 案例 `p1-rst-daemon` 之 Windows 面）。

## 前置條件

1. **環境與權限**：
   - Windows 11 本機主機，具備排程工作管理權限。
   - 隔離測試實例（獨立 `%LOCALAPPDATA%\sw-throwaway\<run_id>\`），不影響正式生產服務。
   - Throwaway 工作已註冊，工作名稱為 `$TaskName = "serialwrap-throwaway-<run_id>"`。
   - 設置隔離環境變數（OD-15）與專用測試端點（`tcp://127.0.0.1:48701`，非生產 48700）：
     ```powershell
     $InstallRoot = "$env:LOCALAPPDATA\sw-throwaway\$RunId"
     $env:SERIALWRAP_STATE_DIR = "$InstallRoot\state"
     $env:SERIALWRAP_RUN_DIR = "$InstallRoot\run"
     $env:SERIALWRAP_CONFIG_DIR = "$InstallRoot\config"
     $env:SERIALWRAP_TEST_DEVICE_ALLOWLIST = "COM20"
     $env:SW_TEST_ALLOW_SCHTASKS = "1"
     $Endpoint = "tcp://127.0.0.1:48701"
     ```

2. **服務狀態**：
   - 舊 daemon 正常處於 `READY` 狀態，且無進行中之 Flash 或檔案傳輸。
   - 監管定義確認具備 `RestartOnFailure(PT1M, 999)`。

## 步驟

1. **觸發有序關機與重啟（Graceful Restart）**：
   透過 Rust CLI 於隔離端點執行優雅重啟指令：
   ```powershell
   serialwrap --endpoint $Endpoint --timeout 10.0 daemon restart
   ```
   CLI 將依序執行：
   - 暫時抑制監管自動重啟（`hold_autostart`）。
   - 向執行中 daemon 發送 `daemon.stop` RPC（等候有序關機，上限 2.0 s）。
   - 驗證舊行程及其子行程完全消失（上限 10.0 s，SC-12）。
   - 重新解除抑制並由 Task Scheduler 或 CLI 拉起新版本 daemon。

2. **模擬異常崩潰重啟（Failure Recovery）**：
   定位隔離實例所屬之 launcher PID，針對該特定 PID 執行強制終止以模擬崩潰（嚴禁使用 `Stop-Process -Name` 影響同主機其他程序）：
   ```powershell
   $procs = Get-CimInstance Win32_Process -Filter "Name = 'serialwrapd.exe'"
   $targetProc = $procs | Where-Object { $_.ExecutablePath -like "*sw-throwaway*" }
   $launcherPid = $targetProc.ProcessId
   Stop-Process -Id $launcherPid -Force
   ```
   觀測 Task Scheduler 根據 XML 的 `RestartOnFailure` 設定在 1 分鐘後拉起新行程。

3. **紀錄狀態與時間戳**：
   使用證據收集腳本記錄終止時間與新實例 `daemon status` 回應的時間戳。

## 預期證據

產出的證據檔 `p1-rst-daemon.json` 須包含：
- 終止前與重啟後的 PID 快照與程序樹結構。
- 優雅停止交接期間無未授權重啟或雙重開啟（two-reader window = 0）。
- 異常崩潰重啟時間（`relaunch_seconds ≤ 90`）。
- 重啟後 session 狀態機與 WAL seq 正確銜接。

## 判定

判定器執行：
```bash
python3 scripts/rust/judge_spike2.py --evidence-dir <evidence_dir> --require-case p1-rst-daemon
```

- **PASS**：舊 daemon 關閉至新 daemon 就緒期間無衝突，重啟時間 ≤ 90 秒，重啟後 RPC 正常回應且狀態一致。
- **FAIL**：重啟超時（> 90 秒）、出現多個常駐實例、或優雅停止被破壞。
- **INSUFFICIENT_EVIDENCE**：缺漏崩潰或重啟觀測點，或工作身分未符合 throwaway 規範。

## 清理

測試結束後：
1. 終止任何殘留之測試 daemon 行程。
2. 移除 Throwaway 排程工作並清理隔離目錄：
   ```powershell
   $TaskName = "serialwrap-throwaway-$RunId"
   schtasks /End /TN "$TaskName" 2>$null
   schtasks /Delete /TN "$TaskName" /F
   if ($InstallRoot -and (Test-Path $InstallRoot)) {
       Remove-Item -Path $InstallRoot -Recurse -Force
   }
   ```
3. 移除隔離狀態與 WAL 目錄。
