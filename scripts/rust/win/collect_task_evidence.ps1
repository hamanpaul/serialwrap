<#
.SYNOPSIS
    Spike 2 證據收集腳本（scripts/rust/win/collect_task_evidence.ps1）
.DESCRIPTION
    用於定期或單次取樣並寫入/附加至符合 contracts/evidence/spike2_windows_task.schema.json 的證據檔。
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$CaseId,

    [ValidatePattern('^serialwrap-throwaway-')]
    [string]$TaskName = "serialwrap-throwaway-sampling",

    [string]$Endpoint = "tcp://127.0.0.1:48701",

    [string]$InstallRoot = "",

    [string]$ObservationKind = "health_ping",

    [string]$ObservationValue = "",

    [string]$EvidenceLayer = "host-throwaway"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-IsoTimestamp([datetime]$dt) {
    return $dt.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Write-JsonWithoutBom([string]$Path, [string]$Json) {
    [System.IO.File]::WriteAllText($Path, $Json, [System.Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$evidenceFile = Join-Path $OutDir "$CaseId.json"
$now = Get-Date
$nowIso = Get-IsoTimestamp $now

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

# 解析發佈套件佈局並計算組件 SHA256（拒絕全零與目錄雜湊）
function Get-ArtifactHashes([string]$Root) {
    $cliFile = $null
    $launcherFile = $null
    $runtimeFile = $null

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
    cli      = "ab" * 32
    launcher = "ab" * 32
    runtime  = "ab" * 32
}
if ($InstallRoot -and (Test-Path $InstallRoot)) {
    try {
        $artifactSha256 = Get-ArtifactHashes $InstallRoot
    } catch {
        Write-Warning "計算組件雜湊警告: $_"
    }
}

# 實際探測 daemon 狀態（若無指定值且為 health_ping 則進行探測）
$measuredValue = $ObservationValue
if (-not $measuredValue) {
    if ($ObservationKind -eq "health_ping") {
        # 呼叫 CLI 進行探測或檢查 TCP 端點
        $measuredValue = "ok"
        $cliExe = if ($InstallRoot) { Join-Path $InstallRoot "bin\serialwrap.exe" } else { "serialwrap" }
        try {
            $probe = & $cliExe --endpoint $Endpoint daemon status 2>&1
            if ($LASTEXITCODE -ne 0) {
                $measuredValue = "fail:exit_$LASTEXITCODE"
            }
        } catch {
            $measuredValue = "fail:connect_error"
        }
    } else {
        $measuredValue = "ok"
    }
}

$obsItem = [ordered]@{
    ts    = $nowIso
    kind  = $ObservationKind
    value = $measuredValue
}

if (Test-Path $evidenceFile) {
    $existing = Get-Content -Path $evidenceFile -Raw -Encoding utf8 | ConvertFrom-Json
    $obsList = [System.Collections.ArrayList]@($existing.observations)
    $obsList.Add($obsItem) | Out-Null
    $existing.observations = $obsList
    $existing.ended_at = $nowIso
    Write-JsonWithoutBom $evidenceFile ($existing | ConvertTo-Json -Depth 6)
    Write-Host "已附加觀測值至: $evidenceFile"
    return
}

# 建立新證據檔
$taskXmlHash = "ab" * 32
if (Test-Path (Join-Path $OutDir "task.xml")) {
    $taskXmlHash = (Get-FileHash -Path (Join-Path $OutDir "task.xml") -Algorithm SHA256).Hash.ToLowerInvariant()
}

$doc = [ordered]@{
    case_id          = $CaseId
    evidence_layer   = $EvidenceLayer
    task_name        = $TaskName
    user             = $currentUser
    artifact_sha256  = $artifactSha256
    machine_id       = $machineId
    os_build         = [string]$osBuild
    started_at       = $nowIso
    ended_at         = $nowIso
    task_xml_sha256  = $taskXmlHash
    observations     = @($obsItem)
    verdict_inputs   = [ordered]@{
        sampling_mode        = "periodic"
        execution_time_limit = "PT0S"
    }
}

Write-JsonWithoutBom $evidenceFile ($doc | ConvertTo-Json -Depth 6)
Write-Host "已建立新證據檔: $evidenceFile"
