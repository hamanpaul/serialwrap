# Stage 0 Windows throwaway 排程監管程序

本程序在擁有者 Windows host 上驗證 Python 基線的 Task Scheduler 監管行為。throwaway 工作與資料皆使用唯一 run_id；證據標記為 host-throwaway，不得用來滿足正式工作站的 windows-task、L2、L3 或 bench 長跑要求。

本程序只操作本次 run_id 的 throwaway 工作與資料，不接觸正式 serialwrap 工作、使用者設定／state 或 production daemon。長跑使用專用 com0com pair COM24:COM25，不開啟實體 COM 裝置。

| 欄位 | 內容 |
|---|---|
| entry | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1`；只以新 run_id 執行 |
| revision | `git rev-parse HEAD:realhw/procedures/stage0-win-task-throwaway.md`；另記 `win-task-throwaway.ps1` 與 `spike1_remaining.py` 的 SHA-256 |
| expected | 隔離目錄與 loopback endpoint、COM24 allowlist、Task Scheduler 登入觸發／失敗重啟、連續執行超過 72 小時、5 分鐘唯讀健康輪詢、完整 task history 與 cleanup 證據 |
| owner | 擁有者 Windows host；Task Scheduler Operational log 若需啟用，由擁有者處理權限 |
| evidence_dir | Windows `%LOCALAPPDATA%\sw-throwaway\<run_id>\log\`；索引報告為 `evidence/stage0/owner/p2-t18/win-task-throwaway-72h.json` |
| rollback | 執行同一 run_id 的 `-Cleanup`；只停止／移除該 throwaway 工作，確認沒有帶該 run 專屬 `daemon.lock` 參數的 `serialwrapd.exe`，保留目錄供證據查核 |

## 前置條件

- 由擁有者在 Windows host 執行，Python 基線及已核准的 Windows bundle 已準備好。
- Task 18 計畫指定以 `scripts/rust/mkbundle.sh --target <Windows triple> --stage 0` 產生 Windows bundle，且使用 Python 基線入口。此 P2 worktree 尚無 P3 的 `scripts/rust/mkbundle.sh`；在 P3 交付前，使用現有 `scripts/build_windows.ps1` 產生 Python bundle，內含 `serialwrapd.exe`。不得把其他版本的程式混進本次證據。
- run_id 使用 UTC 時間加六位小寫十六進位隨機值，格式為 YYYYMMDDTHHMMSSZ-xxxxxx。每次執行都使用新值。
- 目標 pair 固定為 DUT-side COM20:COM21；serialwrap 的受測端為 COM20，對端為 COM21。
- Spike 1 工具只接受 DUT-side COM20:COM21 與 Console-side COM22:COM23；不允許用其他 COM 號替代。
- 本次 72 h throwaway 工作固定使用 owner 於 2026-10-20 建立的專用 com0com pair：CNCA2=COM24（serialwrap daemon 端）、CNCB2=COM25（peer）。建立記錄為 `~/b-log/rust-refactor/spike1/setupc-install3.txt`。
- COM24:COM25 專供這次 Windows Task Scheduler throwaway 長跑；COM20:COM21 保留給 Rust daemon 原生 serial gates，COM22:COM23 保留給其他 gates。不得以 `-AllowCom` 指向其他埠。
- 僅使用上述虛擬 pair，不要連接或開啟任何實體序列裝置。Spike 1 的 COM20／COM21 owner allowlist 與 HVCI 前置檢查仍依原程序辦理。
- 在啟動 throwaway daemon 前完成 G1～G8、Spike 1 控制線與 peer-close 實測；daemon 執行期間會獨占 COM24。若須重測其他 pair，先執行本 run 的 `-Cleanup`，並確認 throwaway process 已結束。
- 若執行原生 pytest，WSL 端明確傳入 `SW_TEST_COM_PAIR=COM20:COM21`；Console-side pair 可選，擁有者 host 已建立時傳入 `SW_TEST_CONSOLE_COM_PAIR=COM22:COM23`。沒有 pair 設定時測試不得自行探測其他 COM 埠。

## 建立與啟動 throwaway 工作

1. 從 repo 根目錄在 PowerShell 7（`pwsh.exe`）建立 Python 基線 bundle；`build_windows.ps1` 的 exe smoke-test 參數 `--help` 在 Windows PowerShell 5.1 會觸發 parser error。若 PyInstaller 已存在就不安裝或升級；若缺少，才裝入 Python 3.12 的使用者 site，使用 2025-08-03 發布的 PyInstaller 6.15.0。

    $repoRoot = (Get-Location).Path
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PIP_CACHE_DIR = Join-Path $repoRoot "dist\pip-cache"
    $env:TEMP = Join-Path $repoRoot "build\win-temp"
    $env:TMP = $env:TEMP
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $repoRoot "build\pyinstaller-config"
    $pyInstallerVersion = py -3.12 -c "import importlib.metadata as m; print(m.version('PyInstaller'))" 2>$null
    if ($LASTEXITCODE -ne 0) {
      py -3.12 -m pip install --user --disable-pip-version-check pyinstaller==6.15.0
      $pyInstallerVersion = py -3.12 -c "import importlib.metadata as m; print(m.version('PyInstaller'))"
    }
    if ([version]$pyInstallerVersion -lt [version]"6.0") {
      throw "Existing PyInstaller is older than the build requirement; refusing to upgrade it."
    }
    function global:python { & py -3.12 @args }
    $env:PIP_USER = "1"
    try {
      & .\scripts\build_windows.ps1
    } finally {
      Remove-Item Function:\global:python -ErrorAction SilentlyContinue
      Remove-Item Env:PIP_USER -ErrorAction SilentlyContinue
      Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
      Remove-Item Env:PIP_CACHE_DIR -ErrorAction SilentlyContinue
      Remove-Item Env:TEMP -ErrorAction SilentlyContinue
      Remove-Item Env:TMP -ErrorAction SilentlyContinue
      Remove-Item Env:PYINSTALLER_CONFIG_DIR -ErrorAction SilentlyContinue
    }
    $bundle = (Resolve-Path .\dist).Path
    Get-FileHash (Join-Path $bundle "serialwrap.exe") -Algorithm SHA256
    Get-FileHash (Join-Path $bundle "serialwrapd.exe") -Algorithm SHA256

   取得兩個 SHA-256 後，再產生本次新的 run id：

    $randomBytes = New-Object byte[] 3
    $randomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $randomSource.GetBytes($randomBytes)
    $randomSource.Dispose()
    $suffix = [BitConverter]::ToString($randomBytes).Replace("-", "").ToLowerInvariant()
    $runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + $suffix

   P3 提供 `scripts/rust/mkbundle.sh` 後，依其 Windows target 與 Stage 0 Python-baseline 選項建置 bundle；本工作樹未含該腳本，不能在這裡宣稱已執行 mkbundle。

2. 先只輸出工作 XML 與環境，人工確認 URI、執行身分、重啟設定、資料目錄和 endpoint：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Hours 73 -Bundle $bundle -AllowCom COM24 -WhatIf

   -WhatIf 不建立目錄、不呼叫 Task Scheduler，也不呼叫 schtasks。必須確認：

   - Task 名稱為 serialwrap-throwaway-<run_id>，URI 不指向正式 serialwrap。
   - ExecutionTimeLimit 是 PT0S；Trigger 是目前使用者登入；失敗後每分鐘重試，最多 999 次。
   - state/run/config/profiles/WAL/log/data/events/events-runtime/by-id/by-path 均在 `%LOCALAPPDATA%\sw-throwaway\<run_id>\` 內；`SERIALWRAP_ENDPOINT` 與 `SERIALWRAP_SOCKET` 指向同一個 run 專屬 loopback port，且不是 48700。
   - `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM24`，值只能是明確允許的 COM24。Launcher 會先清除所有繼承的 `SERIALWRAP_*` 變數，再只套用列出的隔離設定。

3. 確認 run id 尚未使用後才註冊。註冊會建立該 run 專屬目錄與 launcher，不會覆寫既有目錄或同名工作：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Hours 73 -Bundle $bundle -AllowCom COM24
    Start-ScheduledTask -TaskName "serialwrap-throwaway-$runId"
    $startedAt = [DateTime]::UtcNow
    $taskName = "serialwrap-throwaway-$runId"
    $root = Join-Path $env:LOCALAPPDATA "sw-throwaway\$runId"
    $logDir = Join-Path $root "log"
    & .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Hours 73 -Bundle $bundle -AllowCom COM24 -WhatIf |
      Set-Content -LiteralPath (Join-Path $logDir "whatif-output.txt") -Encoding UTF8
    $cli = Join-Path $bundle "serialwrap.exe"
    $port = 49152 + ([Convert]::ToInt32($runId.Substring($runId.Length - 6), 16) % 16384)
    $endpoint = "tcp://127.0.0.1:$port"
    $statusOutput = & $cli --endpoint $endpoint daemon status
    $statusExit = $LASTEXITCODE
    $initialStatus = $statusOutput | ConvertFrom-Json
    if ($statusExit -ne 0 -or -not $initialStatus.ok -or -not $initialStatus.test_device_allowlist.enabled -or
        @($initialStatus.test_device_allowlist.coms).Count -ne 1 -or
        $initialStatus.test_device_allowlist.coms[0] -cne "COM24") {
      throw "throwaway daemon status did not confirm the isolated COM24 allowlist"
    }
    $initialStatus | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $logDir "health-status-initial.json") -Encoding UTF8

   Launcher 將隔離目錄、COM24 allowlist 與專屬 TCP endpoint 設在 daemon process 環境，並明確帶入該 run 的 profile、socket 與 lock 路徑。stdout/stderr 分開寫入帶唯一 token 的原始 log 檔。`-Hours` 是實際監管期限：到期會停止該 daemon 並以 exit 0 結束；期限前意外正常退出會回報失敗，讓 Task Scheduler 依設定重啟。排程工作以目前使用者身分執行，不要求提升權限。

## 監管與證據

- 從 Start-ScheduledTask 成功啟動時間起連續觀察至少 73 小時，不能把 72 小時整或不連續片段當成 >72 h。
- 以 run_id 後六碼計算專屬 port，使用 bundle 中的 serialwrap.exe 明確指定 endpoint。先用唯讀 `daemon status` 確認 daemon 存活，並確認回應的 `test_device_allowlist.coms` 恰為 `["COM24"]`。以下建立隱藏、獨立的 PowerShell poller；其輸出只寫進本次 throwaway log 目錄：

    $port = 49152 + ([Convert]::ToInt32($runId.Substring($runId.Length - 6), 16) % 16384)
    $endpoint = "tcp://127.0.0.1:$port"
    $log = Join-Path $logDir "health-status.ndjson"
    $pollScript = Join-Path $logDir "health-poller.ps1"
    @'
    param([string]$Endpoint, [string]$Cli, [string]$Log, [string]$StopAtUtc)
    $stopAt = [DateTime]::Parse($StopAtUtc).ToUniversalTime()
    while ([DateTime]::UtcNow -lt $stopAt) {
      $started = [DateTime]::UtcNow
      $output = (& $Cli --endpoint $Endpoint daemon status 2>&1 | Out-String).Trim()
      $row = [ordered]@{ observed_at = $started.ToString("o"); endpoint = $Endpoint; exit_code = $LASTEXITCODE; output = $output }
      (ConvertTo-Json -InputObject $row -Compress) | Add-Content -LiteralPath $Log -Encoding UTF8
      $wait = 300 - [int]([DateTime]::UtcNow - $started).TotalSeconds
      if ($wait -gt 0) { Start-Sleep -Seconds $wait }
    }
    '@ | Set-Content -LiteralPath $pollScript -Encoding UTF8
    $stopAt = $startedAt.AddHours(73).ToUniversalTime().ToString("o")
    $pollerArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$pollScript`" -Endpoint `"$endpoint`" -Cli `"$cli`" -Log `"$log`" -StopAtUtc `"$stopAt`""
    $poller = Start-Process powershell -WindowStyle Hidden -PassThru -ArgumentList $pollerArgs
    $poller.Id | Set-Content -LiteralPath (Join-Path $logDir "health-poller.pid") -Encoding ASCII

   記錄 `$poller.Id` 與啟動時間；不得等待或停止此 process。背景程式每 5 分鐘僅執行指定 endpoint 的唯讀 `daemon status`，直到 `$startedAt + 73h`。

  daemon status 只送唯讀 RPC health.status。不得使用未帶該 endpoint 的預設 CLI，也不得對正式 daemon 發送有副作用的 RPC。保存輪詢過程與完整輸出；每列包含 UTC 時間、endpoint、exit code 與回應。
- 開跑前啟用並確認 `Microsoft-Windows-TaskScheduler/Operational` log；若目前關閉，擁有者執行 `wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true` 後再次查詢。每次觀察保存 `Get-ScheduledTaskInfo -TaskName $taskName` 的 last result 與排程狀態，結束時匯出觀察期間內與 task name 相符的 Operational events：

    $taskName = "serialwrap-throwaway-$runId"
    $logDir = Split-Path -Parent $log
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
    $taskInfo | Select-Object LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns |
      ConvertTo-Json -Compress | Add-Content -LiteralPath $log -Encoding UTF8
    $events = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-TaskScheduler/Operational'; StartTime = $startedAt } |
      Where-Object { $_.Message -like "*$taskName*" }
    $events | Select-Object TimeCreated, Id, LevelDisplayName, Message |
      ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logDir 'task-history.json') -Encoding UTF8
    $actionStarts = @($events | Where-Object { $_.Id -eq 200 }).Count
    $restartCount = [Math]::Max(0, $actionStarts - 1)

  將 `LastTaskResult`、`restartCount`、事件檔與 log 是否可讀寫一併記入報告。無法啟用或取得 history 時，`task_history` 必須記 `NOT_RUN`，不能報整體 PASS。
- 保存每筆輪詢的 UTC 時間、RPC endpoint、完整回應與錯誤；另保存 Task Scheduler 的執行狀態／last result、daemon log、實際 restart 次數及所有觀察缺口。
- 證據報告依 `realhw/acceptance/win_task_throwaway_report.schema.json` 驗證，至少記錄 run_id、Task 名稱、使用者 SID、bundle revision／hash、所有隔離路徑、allowlist、專屬 RPC port、註冊 XML 及 SHA-256、開始／結束時間、有效持續秒數、輪詢間隔與缺漏、Task Scheduler 重啟資料，以及 `evidence_layer: "host-throwaway"`、`uart_kind: "native"`、非空 `results[]`。
- 報告放在 evidence/stage0/owner/p2-t18/win-task-throwaway-72h.json，登錄到 S0-WIN-TASK-THROWAWAY-72H。只有實際連續超過 72 小時且證據完整才可填 present；此結果仍不能取代正式工作站 windows-task 證據。

## 清除

先完成最後一次輪詢並保存證據，再只清除此 run 的排程工作：

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Cleanup
    Get-ScheduledTask -TaskName "serialwrap-throwaway-$runId" -ErrorAction SilentlyContinue

查詢結果應為空。`-Cleanup` 只停止／移除名稱完全符合該 run id 的 throwaway 工作，並確認沒有 command line 帶本次隔離 `daemon.lock` 的 `serialwrapd.exe` 留存；它保留隔離目錄與 log 供證據查核。不要以通配符刪除工作或目錄。此檢查不會另開 COM 埠；COM20:COM21 與 COM22:COM23 仍依各自原生 gate 管理。將報告與 log 收存完成後，再依擁有者的證據保存規則處理資料。
