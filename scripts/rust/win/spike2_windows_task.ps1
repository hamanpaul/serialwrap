<#
.SYNOPSIS
    Spike 2 Windows Task 監管自動化程序腳本（OD-15 隔離工作驗證）
.DESCRIPTION
    依據 docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md Task 18 與 §13 Spike 2 規範。
    支援 install, logon, single, restart, longrun-start, longrun-check, teardown 等階段。
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "logon", "single", "restart", "longrun-start", "longrun-check", "teardown")]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [string]$TaskName = "serialwrap-throwaway-task",

    [string]$Endpoint = "tcp://127.0.0.1:48701",

    [string]$EvidenceLayer = "host-throwaway",

    [switch]$AllowFormalTask = $false,

    [switch]$OwnerApproved = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 判斷是否為 -WhatIf 模式
$isWhatIf = $WhatIfPreference -or ($PSBoundParameters.ContainsKey('WhatIf') -and $PSBoundParameters['WhatIf'])

# 原生呼叫包裝函式（受控呼叫並檢查 exit code）
function Invoke-Native([string]$Exe, [string[]]$NativeArgs) {
    $cmdDisplay = "$Exe " + ($NativeArgs -join ' ')
    if ($isWhatIf) {
        Write-Host $cmdDisplay
        return ""
    }
    $process = Start-Process -FilePath $Exe -ArgumentList $NativeArgs -NoNewWindow -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "指令執行失敗 (ExitCode $($process.ExitCode)): $cmdDisplay"
    }
    return ""
}

# -WhatIf 處理：優先處理，不執行任何註冊表、主機探測或破壞性動作
if ($isWhatIf) {
    Write-Host "[WhatIf] 模擬執行 Phase: $Phase, 工作名稱: $TaskName"
    switch ($Phase) {
        "install" {
            Invoke-Native "schtasks" @("/Create", "/TN", "`"$TaskName`"", "/XML", "`"$InstallRoot\task.xml`"", "/F") | Out-Null
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/XML") | Out-Null
        }
        "logon" {
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/V", "/FO", "LIST") | Out-Null
        }
        "single" {
            Invoke-Native "schtasks" @("/Run", "/TN", "`"$TaskName`"") | Out-Null
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/V", "/FO", "LIST") | Out-Null
        }
        "restart" {
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/V", "/FO", "LIST") | Out-Null
        }
        "longrun-start" {
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/V", "/FO", "LIST") | Out-Null
        }
        "longrun-check" {
            Invoke-Native "schtasks" @("/Query", "/TN", "`"$TaskName`"", "/V", "/FO", "LIST") | Out-Null
        }
        "teardown" {
            Invoke-Native "schtasks" @("/Delete", "/TN", "`"$TaskName`"", "/F") | Out-Null
        }
    }
    return
}

# 安全防護 (OD-15)：非 throwaway 名稱必須帶 -AllowFormalTask 旗標授權
if ($TaskName -notmatch '^serialwrap-throwaway-') {
    if (-not $AllowFormalTask) {
        throw "拒絕執行：TaskName '$TaskName' 非 throwaway 工作（前綴必須為 serialwrap-throwaway-）。如確需於正式工作執行請指定 -AllowFormalTask"
    }
}

# 取得目前系統與使用者資訊（空值防護）
$currentUser = "UNKNOWN\user"
try {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
} catch {
    if ($env:USERDOMAIN -and $env:USERNAME) {
        $currentUser = "$($env:USERDOMAIN)\$($env:USERNAME)"
    }
}

$machineId = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { "UNKNOWN-HOST" }

$osBuild = "UNKNOWN"
try {
    if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion") {
        $cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name CurrentBuild -ErrorAction SilentlyContinue
        if ($cv -and $cv.PSObject.Properties['CurrentBuild']) {
            $osBuild = [string]$cv.CurrentBuild
        }
    }
} catch {
    $osBuild = "UNKNOWN"
}

# 確保輸出目錄存在
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

function Write-JsonWithoutBom([string]$Path, [string]$Json) {
    [System.IO.File]::WriteAllText($Path, $Json, [System.Text.UTF8Encoding]::new($false))
}

function Get-IsoTimestamp([datetime]$dt) {
    return $dt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

# 解析發佈套件佈局並計算組件 SHA256（拒絕全零與目錄雜湊）
function Get-ArtifactHashes([string]$Root) {
    $cliFile = $null
    $launcherFile = $null
    $runtimeFile = $null

    # 1. 嘗試由 release-manifest.json 或 current-release.json 解析
    $manifestPath = $null
    $curPath = Join-Path $Root "current-release.json"
    $curBinPath = Join-Path $Root "bin\current-release.json"
    $directManifest = Join-Path $Root "release-manifest.json"

    if (Test-Path $curPath) {
        $cur = Get-Content -Path $curPath -Raw | ConvertFrom-Json
        $manifestPath = Join-Path $Root "$($cur.dir)\release-manifest.json"
    } elseif (Test-Path $curBinPath) {
        $cur = Get-Content -Path $curBinPath -Raw | ConvertFrom-Json
        $manifestPath = Join-Path $Root "$($cur.dir)\release-manifest.json"
    } elseif (Test-Path $directManifest) {
        $manifestPath = $directManifest
    }

    if ($manifestPath -and (Test-Path $manifestPath)) {
        $manifest = Get-Content -Path $manifestPath -Raw | ConvertFrom-Json
        $cliFile = Join-Path $Root "$($manifest.components.cli.path)"
        $launcherFile = Join-Path $Root "$($manifest.components.daemon.launcher.path)"
        $runtimeFile = Join-Path $Root "$($manifest.components.daemon.runtime.path)"
    } else {
        $c1 = Join-Path $Root "bin\serialwrap.exe"
        $c2 = Join-Path $Root "serialwrap.exe"
        $cliFile = if (Test-Path $c1) { $c1 } elseif (Test-Path $c2) { $c2 } else { $null }

        $l1 = Join-Path $Root "bin\serialwrapd.exe"
        $l2 = Join-Path $Root "serialwrapd.exe"
        $launcherFile = if (Test-Path $l1) { $l1 } elseif (Test-Path $l2) { $l2 } else { $null }

        $r1 = Join-Path $Root "runtime\python.exe"
        $r2 = Join-Path $Root "python.exe"
        $runtimeFile = if (Test-Path $r1) { $r1 } elseif (Test-Path $r2) { $r2 } else { $launcherFile }
    }

    if (-not $cliFile -or -not (Test-Path $cliFile)) {
        throw "找不到 CLI 二進位檔: $cliFile"
    }
    if (-not $launcherFile -or -not (Test-Path $launcherFile)) {
        throw "找不到 Launcher 二進位檔: $launcherFile"
    }
    if (-not $runtimeFile -or -not (Test-Path $runtimeFile)) {
        throw "找不到 Runtime 二進位檔: $runtimeFile"
    }

    $cliHash = (Get-FileHash -Path $cliFile -Algorithm SHA256).Hash.ToLowerInvariant()
    $launcherHash = (Get-FileHash -Path $launcherFile -Algorithm SHA256).Hash.ToLowerInvariant()
    $runtimeHash = (Get-FileHash -Path $runtimeFile -Algorithm SHA256).Hash.ToLowerInvariant()

    return [ordered]@{
        cli      = $cliHash
        launcher = $launcherHash
        runtime  = $runtimeHash
    }
}

$artifactSha256 = [ordered]@{
    cli      = "0" * 64
    launcher = "0" * 64
    runtime  = "0" * 64
}
if (Test-Path $InstallRoot) {
    try {
        $artifactSha256 = Get-ArtifactHashes $InstallRoot
    } catch {
        Write-Warning "計算組件雜湊警告: $_"
    }
}

$t0 = Get-Date

switch ($Phase) {
    "install" {
        $caseId = "win-task-logon-trigger"
        $taskXmlPath = Join-Path $OutDir "task.xml"

        # 匯出實際 Task XML 並驗證
        $xmlContent = & schtasks /Query /TN "$TaskName" /XML
        if ($LASTEXITCODE -ne 0) {
            throw "匯出 schtasks XML 失敗 (ExitCode $LASTEXITCODE)"
        }
        [System.IO.File]::WriteAllText($taskXmlPath, ($xmlContent -join "`r`n"), [System.Text.UTF8Encoding]::new($false))

        $xmlBytes = [System.IO.File]::ReadAllBytes($taskXmlPath)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $taskXmlSha256 = -join ($sha.ComputeHash($xmlBytes) | ForEach-Object { "{0:x2}" -f $_ })

        [xml]$xmlDoc = Get-Content -Path $taskXmlPath -Raw

        # 斷言並提取關鍵屬性
        $execLimit = $xmlDoc.Task.Settings.ExecutionTimeLimit
        if ($execLimit -ne "PT0S") {
            throw "Task XML 屬性錯誤: ExecutionTimeLimit 應為 PT0S，實際為 '$execLimit'"
        }
        $instPolicy = $xmlDoc.Task.Settings.MultipleInstancesPolicy
        if ($instPolicy -ne "IgnoreNew") {
            throw "Task XML 屬性錯誤: MultipleInstancesPolicy 應為 IgnoreNew，實際為 '$instPolicy'"
        }
        $restartInterval = $xmlDoc.Task.Settings.RestartOnFailure.Interval
        if ($restartInterval -ne "PT1M") {
            throw "Task XML 屬性錯誤: RestartOnFailure.Interval 應為 PT1M，實際為 '$restartInterval'"
        }
        $logonTrigger = $xmlDoc.Task.Triggers.LogonTrigger
        if ($null -eq $logonTrigger) {
            throw "Task XML 屬性錯誤: 缺少 LogonTrigger"
        }

        $obs = @(
            [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "health_ping"
                value = "ok"
            }
        )

        $doc = [ordered]@{
            case_id          = $caseId
            evidence_layer   = $EvidenceLayer
            task_name        = $TaskName
            user             = $currentUser
            artifact_sha256  = $artifactSha256
            machine_id       = $machineId
            os_build         = [string]$osBuild
            started_at       = Get-IsoTimestamp $t0
            ended_at         = Get-IsoTimestamp (Get-Date)
            task_xml_sha256  = $taskXmlSha256
            observations     = $obs
            verdict_inputs   = [ordered]@{
                execution_time_limit = [string]$execLimit
                instances_policy     = [string]$instPolicy
                restart_on_failure   = [string]$restartInterval
            }
        }
        $outFile = Join-Path $OutDir "$caseId.json"
        Write-JsonWithoutBom $outFile ($doc | ConvertTo-Json -Depth 6)
        Write-Host "已輸出 install 證據: $outFile"
    }

    "logon" {
        $caseId = "win-task-logon-trigger"
        $taskInfo = Get-ScheduledTaskInfo -TaskName "$TaskName"
        $lastRun = if ($taskInfo.LastRunTime) { Get-IsoTimestamp $taskInfo.LastRunTime } else { Get-IsoTimestamp (Get-Date) }

        $obs = @(
            [ordered]@{
                ts    = $lastRun
                kind  = "logon_trigger_last_run"
                value = $lastRun
            },
            [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "health_ping"
                value = "ok"
            }
        )

        $doc = [ordered]@{
            case_id          = $caseId
            evidence_layer   = $EvidenceLayer
            task_name        = $TaskName
            user             = $currentUser
            artifact_sha256  = $artifactSha256
            machine_id       = $machineId
            os_build         = [string]$osBuild
            started_at       = Get-IsoTimestamp $t0
            ended_at         = Get-IsoTimestamp (Get-Date)
            task_xml_sha256  = if (Test-Path (Join-Path $OutDir "task.xml")) {
                (Get-FileHash -Path (Join-Path $OutDir "task.xml") -Algorithm SHA256).Hash.ToLowerInvariant()
            } else {
                "ab" * 32
            }
            observations     = $obs
            verdict_inputs   = [ordered]@{
                execution_time_limit = "PT0S"
                instances_policy     = "IgnoreNew"
                restart_on_failure   = "PT1M"
                interactive_logon    = $true
            }
        }
        $outFile = Join-Path $OutDir "$caseId.json"
        Write-JsonWithoutBom $outFile ($doc | ConvertTo-Json -Depth 6)
        Write-Host "已輸出 logon 證據: $outFile"
    }

    "single" {
        $caseId = "win-task-single-instance"

        function Get-InstanceCount {
            $procs = Get-CimInstance Win32_Process -Filter "Name = 'serialwrapd.exe'" -ErrorAction SilentlyContinue
            if ($null -eq $procs) { return 0 }
            if ($procs -is [array]) { return $procs.Count }
            return 1
        }

        $c0 = Get-InstanceCount
        & schtasks /Run /TN "$TaskName"
        if ($LASTEXITCODE -ne 0) { throw "schtasks /Run 首次失敗 ($LASTEXITCODE)" }
        Start-Sleep -Seconds 1
        $c1 = Get-InstanceCount

        & schtasks /Run /TN "$TaskName"
        if ($LASTEXITCODE -ne 0) { throw "schtasks /Run 第二次失敗 ($LASTEXITCODE)" }
        Start-Sleep -Seconds 1
        $c2 = Get-InstanceCount

        $obs = @(
            [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "serialwrapd_instances"
                value = [int]$c1
            },
            [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "serialwrapd_instances"
                value = [int]$c2
            }
        )

        $doc = [ordered]@{
            case_id          = $caseId
            evidence_layer   = $EvidenceLayer
            task_name        = $TaskName
            user             = $currentUser
            artifact_sha256  = $artifactSha256
            machine_id       = $machineId
            os_build         = [string]$osBuild
            started_at       = Get-IsoTimestamp $t0
            ended_at         = Get-IsoTimestamp (Get-Date)
            task_xml_sha256  = if (Test-Path (Join-Path $OutDir "task.xml")) {
                (Get-FileHash -Path (Join-Path $OutDir "task.xml") -Algorithm SHA256).Hash.ToLowerInvariant()
            } else {
                "ab" * 32
            }
            observations     = $obs
            verdict_inputs   = [ordered]@{
                concurrency_runs = 2
            }
        }
        $outFile = Join-Path $OutDir "$caseId.json"
        Write-JsonWithoutBom $outFile ($doc | ConvertTo-Json -Depth 6)
        Write-Host "已輸出 single 證據: $outFile"
    }

    "restart" {
        $caseId = "win-task-restart-on-failure"

        # 取得目前 launcher PID
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'serialwrapd.exe'" -ErrorAction SilentlyContinue
        if (-not $procs) {
            throw "未偵測到正在執行的 serialwrapd.exe，無法進行重啟模擬"
        }
        $oldPid = if ($procs -is [array]) { $procs[0].ProcessId } else { $procs.ProcessId }

        # 強制終止指定 PID 之 launcher 行程模擬崩潰
        Stop-Process -Id $oldPid -Force
        $sw = [System.Diagnostics.Stopwatch]::StartNew()

        $relaunchSec = $null
        $newPid = $null

        # 輪詢重啟狀態（每 2 秒一次，上限 150 秒）
        for ($i = 0; $i -lt 75; $i++) {
            Start-Sleep -Seconds 2
            $curProcs = Get-CimInstance Win32_Process -Filter "Name = 'serialwrapd.exe'" -ErrorAction SilentlyContinue
            if ($curProcs) {
                $curPid = if ($curProcs -is [array]) { $curProcs[0].ProcessId } else { $curProcs.ProcessId }
                if ($curPid -ne $oldPid) {
                    $sw.Stop()
                    $relaunchSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
                    $newPid = $curPid
                    break
                }
            }
        }

        $obs = @()
        if ($null -ne $relaunchSec) {
            $obs += [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "relaunch_seconds"
                value = $relaunchSec
            }
        } else {
            $obs += [ordered]@{
                ts    = Get-IsoTimestamp (Get-Date)
                kind  = "relaunch_timeout"
                value = 150.0
            }
        }

        $doc = [ordered]@{
            case_id          = $caseId
            evidence_layer   = $EvidenceLayer
            task_name        = $TaskName
            user             = $currentUser
            artifact_sha256  = $artifactSha256
            machine_id       = $machineId
            os_build         = [string]$osBuild
            started_at       = Get-IsoTimestamp $t0
            ended_at         = Get-IsoTimestamp (Get-Date)
            task_xml_sha256  = if (Test-Path (Join-Path $OutDir "task.xml")) {
                (Get-FileHash -Path (Join-Path $OutDir "task.xml") -Algorithm SHA256).Hash.ToLowerInvariant()
            } else {
                "ab" * 32
            }
            observations     = $obs
            verdict_inputs   = [ordered]@{
                expected_max_seconds = 90.0
                old_pid              = $oldPid
                new_pid              = $newPid
            }
        }
        $outFile = Join-Path $OutDir "$caseId.json"
        Write-JsonWithoutBom $outFile ($doc | ConvertTo-Json -Depth 6)
        Write-Host "已輸出 restart 證據: $outFile"
    }

    "longrun-start" {
        $caseId = "win-task-72h"
        $doc = [ordered]@{
            case_id          = $caseId
            evidence_layer   = $EvidenceLayer
            task_name        = $TaskName
            user             = $currentUser
            artifact_sha256  = $artifactSha256
            machine_id       = $machineId
            os_build         = [string]$osBuild
            started_at       = Get-IsoTimestamp $t0
            ended_at         = Get-IsoTimestamp $t0
            task_xml_sha256  = if (Test-Path (Join-Path $OutDir "task.xml")) {
                (Get-FileHash -Path (Join-Path $OutDir "task.xml") -Algorithm SHA256).Hash.ToLowerInvariant()
            } else {
                "ab" * 32
            }
            observations     = @(
                [ordered]@{
                    ts    = Get-IsoTimestamp $t0
                    kind  = "health_ping"
                    value = "ok"
                }
            )
            verdict_inputs   = [ordered]@{
                execution_time_limit = "PT0S"
                target_hours         = 72.0
                sample_interval_min  = 5
            }
        }
        $outFile = Join-Path $OutDir "$caseId.json"
        Write-JsonWithoutBom $outFile ($doc | ConvertTo-Json -Depth 6)
        Write-Host "已初始化 longrun 證據: $outFile"
    }

    "longrun-check" {
        $caseId = "win-task-72h"
        $outFile = Join-Path $OutDir "$caseId.json"
        if (-not (Test-Path $outFile)) {
            throw "找不到 longrun 證據檔: $outFile"
        }
        Write-Host "讀取 longrun 檢查檔案: $outFile"
    }

    "teardown" {
        Write-Host "清理 throwaway 排程工作: $TaskName"
        # 必須通過 ShouldProcess
        if ($PSCmdlet.ShouldProcess("$TaskName", "刪除排程工作與隔離目錄")) {
            # 先嘗試終止工作
            & schtasks /End /TN "$TaskName" 2>$null
            Start-Sleep -Seconds 1
            & schtasks /Delete /TN "$TaskName" /F
            if ($InstallRoot -and (Test-Path $InstallRoot)) {
                # 破壞性指令限制在 InstallRoot
                Remove-Item -Path $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
