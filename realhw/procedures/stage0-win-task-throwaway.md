# Stage 0 Windows throwaway 排程監管程序

本程序在擁有者 Windows host 上驗證 Python 基線的 Task Scheduler 監管行為。throwaway 工作與資料皆使用唯一 run_id；證據標記為 host-throwaway，不得用來滿足正式工作站的 windows-task、L2、L3 或 bench 長跑要求。

本次實作只提供程序與工具，不會在目前工作站建立排程工作或啟動 daemon。建立正式 serialwrap 工作、改動使用者設定／state 或連接正式 COM 裝置都不屬於本程序。

| 欄位 | 內容 |
|---|---|
| entry | `pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1`；只以新 run_id 執行 |
| revision | `git rev-parse HEAD:realhw/procedures/stage0-win-task-throwaway.md`；另記 `win-task-throwaway.ps1` 與 `spike1_remaining.py` 的 SHA-256 |
| expected | 隔離目錄與 loopback endpoint、COM20 allowlist、Task Scheduler 登入觸發／失敗重啟、連續執行超過 72 小時、5 分鐘唯讀健康輪詢、完整 task history 與 cleanup 證據 |
| owner | 擁有者 Windows host；Task Scheduler Operational log 若需啟用，由擁有者處理權限 |
| evidence_dir | Windows `%LOCALAPPDATA%\sw-throwaway\<run_id>\log\`；索引報告為 `evidence/stage0/owner/p2-t18/win-task-throwaway-72h.json` |
| rollback | 執行同一 run_id 的 `-Cleanup`；只停止／移除該 throwaway 工作，確認沒有帶該 run 專屬 `daemon.lock` 參數的 `serialwrapd.exe`，保留目錄供證據查核 |

## 前置條件

- 由擁有者在 Windows host 執行，Python 基線及已核准的 Windows bundle 已準備好。
- Task 18 計畫指定以 `scripts/rust/mkbundle.sh --target <Windows triple> --stage 0` 產生 Windows bundle，且使用 Python 基線入口。此 P2 worktree 尚無 P3 的 `scripts/rust/mkbundle.sh`；在 P3 交付前，使用現有 `scripts/build_windows.ps1` 產生 Python bundle，內含 `serialwrapd.exe`。不得把其他版本的程式混進本次證據。
- run_id 使用 UTC 時間加六位小寫十六進位隨機值，格式為 YYYYMMDDTHHMMSSZ-xxxxxx。每次執行都使用新值。
- 目標 pair 固定為 DUT-side COM20:COM21；serialwrap 的受測端為 COM20，對端為 COM21。
- Spike 1 工具只接受 DUT-side COM20:COM21 與 Console-side COM22:COM23；不允許用其他 COM 號替代。
- 僅在已完成 COM20／COM21 的 owner allowlist 與 HVCI 前置檢查後執行。不要連接或開啟其他實體序列裝置。
- 在啟動 throwaway daemon 前完成 G1～G8、Spike 1 控制線與 peer-close 實測；daemon 執行期間會獨占 COM20。若須重測，先執行 `-Cleanup`，再確認 throwaway process 已結束。
- 若執行原生 pytest，WSL 端明確傳入 `SW_TEST_COM_PAIR=COM20:COM21`；Console-side pair 可選，擁有者 host 已建立時傳入 `SW_TEST_CONSOLE_COM_PAIR=COM22:COM23`。沒有 pair 設定時測試不得自行探測其他 COM 埠。

## 建立與啟動 throwaway 工作

1. 從 repo 根目錄在 Windows PowerShell 產生新 run id，並建立 Python 基線 bundle：

    $randomBytes = New-Object byte[] 3
    $randomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $randomSource.GetBytes($randomBytes)
    $randomSource.Dispose()
    $suffix = [BitConverter]::ToString($randomBytes).Replace("-", "").ToLowerInvariant()
    $runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + $suffix
    pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
    $bundle = (Resolve-Path .\dist).Path

   P3 提供 `scripts/rust/mkbundle.sh` 後，依其 Windows target 與 Stage 0 Python-baseline 選項建置 bundle；本工作樹未含該腳本，不能在這裡宣稱已執行 mkbundle。

2. 先只輸出工作 XML 與環境，人工確認 URI、執行身分、重啟設定、資料目錄和 endpoint：

    pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Hours 73 -Bundle $bundle -WhatIf

   -WhatIf 不建立目錄、不呼叫 Task Scheduler，也不呼叫 schtasks。必須確認：

   - Task 名稱為 serialwrap-throwaway-<run_id>，URI 不指向正式 serialwrap。
   - ExecutionTimeLimit 是 PT0S；Trigger 是目前使用者登入；失敗後每分鐘重試，最多 999 次。
   - state/run/config/profiles/WAL/log/data/events/events-runtime/by-id/by-path 均在 `%LOCALAPPDATA%\sw-throwaway\<run_id>\` 內；`SERIALWRAP_ENDPOINT` 與 `SERIALWRAP_SOCKET` 指向同一個 run 專屬 loopback port，且不是 48700。
   - `SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM20`。Launcher 會先清除所有繼承的 `SERIALWRAP_*` 變數，再只套用列出的隔離設定。

3. 確認 run id 尚未使用後才註冊。註冊會建立該 run 專屬目錄與 launcher，不會覆寫既有目錄或同名工作：

    pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Hours 73 -Bundle $bundle
    $startedAt = [DateTime]::UtcNow
    Start-ScheduledTask -TaskName "serialwrap-throwaway-$runId"

   Launcher 將隔離目錄、COM20 allowlist 與專屬 TCP endpoint 設在 daemon process 環境，並明確帶入該 run 的 profile、socket 與 lock 路徑。stdout/stderr 分開寫入帶唯一 token 的原始 log 檔。`-Hours` 是實際監管期限：到期會停止該 daemon 並以 exit 0 結束；期限前意外正常退出會回報失敗，讓 Task Scheduler 依設定重啟。排程工作以目前使用者身分執行，不要求提升權限。

## 監管與證據

- 從 Start-ScheduledTask 成功啟動時間起連續觀察至少 73 小時，不能把 72 小時整或不連續片段當成 >72 h。
- 以 run_id 後六碼計算專屬 port，使用 bundle 中的 serialwrap.exe 明確指定 endpoint；下列監測在目前使用者工作階段執行，輸出只寫進本次 throwaway log 目錄：

    $port = 49152 + ([Convert]::ToInt32($runId.Substring($runId.Length - 6), 16) % 16384)
    $endpoint = "tcp://127.0.0.1:$port"
    $cli = Join-Path $bundle "serialwrap.exe"
    $log = Join-Path $env:LOCALAPPDATA "sw-throwaway\$runId\log\health-status.ndjson"
    $stopAt = [DateTime]::UtcNow.AddHours(73)
    while ([DateTime]::UtcNow -lt $stopAt) {
      $started = [DateTime]::UtcNow
      $output = (& $cli --endpoint $endpoint daemon status 2>&1 | Out-String).Trim()
      $row = [ordered]@{ observed_at = $started.ToString("o"); endpoint = $endpoint; exit_code = $LASTEXITCODE; output = $output }
      (ConvertTo-Json -InputObject $row -Compress) | Add-Content -LiteralPath $log -Encoding UTF8
      $wait = 300 - [int]([DateTime]::UtcNow - $started).TotalSeconds
      if ($wait -gt 0) { Start-Sleep -Seconds $wait }
    }

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

    pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\stage0\win-task-throwaway.ps1 -RunId $runId -Cleanup
    Get-ScheduledTask -TaskName "serialwrap-throwaway-$runId" -ErrorAction SilentlyContinue

查詢結果應為空。`-Cleanup` 只停止／移除名稱完全符合該 run id 的 throwaway 工作，並確認沒有 command line 帶本次隔離 `daemon.lock` 的 `serialwrapd.exe` 留存；它保留隔離目錄與 log 供證據查核。不要以通配符刪除工作或目錄。此檢查不會另開 COM 埠；若需驗證 COM20 可重新開啟，使用已核准的 com0com 原生 gate，在本程序完成後另行量測。將報告與 log 收存完成後，再依擁有者的證據保存規則處理資料。
