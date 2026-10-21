# Spike 2 Windows Task 監管驗證程序

本文件說明 `windows-task` 監管模式的 Spike 2 驗證程序（依據規格 §13 Spike 2 與擁有者裁決 OD-03、OD-15）。涵蓋 `win-task-logon-trigger`、`win-task-single-instance`、`win-task-restart-on-failure` 及 `win-task-72h` 案例之執行與判定。

## 前置條件

1. **作業系統環境**：
   - 擁有者代表主機（Windows 11 Pro，HVCI 開啟）。
   - PowerShell 7（`pwsh`）或 Windows PowerShell 5.1。
   - 工作排程器指令 `schtasks.exe` 可用。

2. **隔離與安全性（OD-15 規範）**：
   - 絕不得變更或刪除生產工作 `serialwrap`。
   - 所有測試工作一律以 Throwaway 模式執行，工作名稱前綴固定為 `serialwrap-throwaway-<run_id>`。
   - 隔離根目錄設定於 `%LOCALAPPDATA%\sw-throwaway\<run_id>\`（含獨立的 state、run 與 config 目錄）。
   - 裝置白名單僅啟用 OD-11 指定之虛擬連接埠（如 `COM20`），避免干擾其他實體串口。

3. **受測組件（Artifacts）**：
   - 完整的 Windows 發行套件目錄（含 `bin/serialwrap.exe`、`bin/serialwrapd.exe` 及 `runtime/`）。
   - 具備有效 SHA256 總和檢查碼。

## 步驟

1. **建立隔離測試環境與 Throwaway 工作**：
   可直接使用 P2 Task 18 throwaway 排程工作程序（`scripts/stage0/win-task-throwaway.ps1 -RunId <id> -Hours 72 -Bundle <dir>`），或以 `scripts/rust/win/spike2_windows_task.ps1` 執行：
   ```powershell
   $RunId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + "-" + (Get-Random -Minimum 0x100000 -Maximum 0xffffff).ToString("x6")
   $TaskName = "serialwrap-throwaway-$RunId"
   $InstallRoot = "$env:LOCALAPPDATA\sw-throwaway\$RunId"
   $OutDir = "target\p4-evidence\spike2-$RunId"
   $Endpoint = "tcp://127.0.0.1:48701"

   # 設置隔離環境變數（OD-15）
   $env:SERIALWRAP_STATE_DIR = "$InstallRoot\state"
   $env:SERIALWRAP_RUN_DIR = "$InstallRoot\run"
   $env:SERIALWRAP_CONFIG_DIR = "$InstallRoot\config"
   $env:SERIALWRAP_TEST_DEVICE_ALLOWLIST = "COM20"
   $env:SW_TEST_ALLOW_SCHTASKS = "1"

   # 執行安裝與 XML 結構驗證
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase install `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint
   ```

2. **驗證工作排程 XML 屬性**：
   檢查匯出的 Task XML（`$OutDir\task.xml`）是否包含以下核心屬性：
   - `ExecutionTimeLimit = PT0S`（關閉 72 小時限制）。
   - `MultipleInstancesPolicy = IgnoreNew`（拒絕重複拉起）。
   - `RestartOnFailure`: `Interval = PT1M`, `Count = 999`。
   - `LogonTrigger`（使用者登入時自啟）。

3. **使用者登入自啟驗證（logon trigger）**：
   由測試人員執行登出／登入後，記錄登入自啟觀測值：
   ```powershell
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase logon `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint
   ```

4. **唯一實例驗證（single instance）**：
   在 daemon 運行中連續觸發多次執行，驗證實例數恆為 1：
   ```powershell
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase single `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint
   ```

5. **異常崩潰自動重啟驗證（restart on failure）**：
   終止指定 launcher PID 行程模擬異常崩潰，觀測工作排程器自動重新拉起的時間：
   ```powershell
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase restart `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint
   ```

6. **72 小時連續運行驗證（longrun）**：
   啟動 72 小時長跑取樣，每 5 分鐘進行一次 `daemon status` 健康探測：
   ```powershell
   # 啟動長跑觀測
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase longrun-start `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint

   # 達到 72 小時後執行驗收檢查
   pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
       -Phase longrun-check `
       -InstallRoot $InstallRoot `
       -OutDir $OutDir `
       -TaskName $TaskName `
       -Endpoint $Endpoint
   ```

## 預期證據

測試產出應放置於 `$OutDir`，檔名對應案例名稱，內容須符合 `contracts/evidence/spike2_windows_task.schema.json`：
- `win-task-logon-trigger.json`：包含 XML SHA256、登入觸發時間與首次健康 ping。
- `win-task-single-instance.json`：包含多次觸發下的實例計數（`serialwrapd_instances = 1`）。
- `win-task-restart-on-failure.json`：包含崩潰終止至重新啟動完成的時間（`relaunch_seconds ≤ 90`）與前後 PID。
- `win-task-72h.json`：包含總運行時長（≥ 72.0 h）與連續取樣紀錄（每 5 分鐘一筆 `health_ping: ok`，無超過 15 分鐘之中斷缺口），且 `execution_time_limit: "PT0S"`。

證據中 `evidence_layer` 必須標示為 `"host-throwaway"`，`task_name` 必須為 `serialwrap-throwaway-*`。

## 判定

使用專案統一判定器執行評估，並將通過結果輸出至 P4 案例驗證目錄：
```bash
python3 scripts/rust/judge_spike2.py \
    --evidence-dir target/p4-evidence/spike2-<run_id> \
    --require-case win-task-72h \
    --require-case win-task-single-instance \
    --require-case win-task-restart-on-failure \
    --require-case win-task-logon-trigger \
    --emit-p4-evidence target/p4-evidence
```

判定門檻依據規格 §11.4 與 OD-15：
- **PASS**：所有必要觀測值齊全、時間跨度 ≥ 72 h 且取樣連續無中斷、重啟時間 ≤ 90 s、實例數恆為 1。
- **FAIL**：總時長不足 72 h、重啟超過 90 s、實例數 > 1，或任一 health ping 非 ok。
- **INSUFFICIENT_EVIDENCE**：缺漏必要欄位、取樣存在 > 15 分鐘缺口、或工作名稱非 throwaway 格式。

## 清理

測試完成後清理所有 throwaway 工作與暫存資料：
```powershell
pwsh -File scripts/rust/win/spike2_windows_task.ps1 `
    -Phase teardown `
    -InstallRoot $InstallRoot `
    -OutDir $OutDir `
    -TaskName $TaskName
```
確認指令 `schtasks /Delete /TN "$TaskName" /F` 已執行，且 `$InstallRoot` 暫存目錄已安全移除。
