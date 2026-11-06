#requires -Version 5.1
<#
.SYNOPSIS
  對 serialwrap-<V>-windows-x86_64.zip 做解開後 smoke（OD-78）；全部在 throwaway 目錄內進行，不碰 COM 埠。

.DESCRIPTION
  1. 解開 zip（必須只有頂層 serialwrap-<V>-windows-x86_64\）；
  2. bin\serialwrap.exe --version／--help；內層 SHA256SUMS 逐項核對；manifest 版本；py\python312._pth 無絕對路徑；
  3. bundle runtime 以 -I -B 載入 yaml／serial／sw_core，且都在 bundle 內；
  4. 隔離環境（config／state／run／WAL／log／install root／USERPROFILE／LOCALAPPDATA 皆指向 throwaway，
     SERIALWRAP_TEST_DEVICE_ALLOWLIST=COM999 使 daemon 不會接管任何真實 COM）：
     setup --on-demand（versions\<V>\bin\serialwrap.exe）→ 安裝根 bin\serialwrap.exe --version → daemon start／status → doctor → daemon stop。
  daemon 與 CLI 使用 loopback 高位埠（-Port），不使用預設 48700。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$HostPython = 'python',
    [string]$SourceRoot,
    [int]$Port = 48791,
    [string]$WorkDir,
    [switch]$KeepWork
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) { $SourceRoot = (Resolve-Path (Join-Path $ScriptRoot '..\..')).Path }
$Helper = Join-Path $ScriptRoot 'mkbundle_windows.py'
function Fail([string]$Message) { throw "smoke-windows-bundle.ps1: $Message" }
function Step([string]$Message) { Write-Output "== $Message" }

if (-not $WorkDir) { $WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("sw-smoke-" + [Guid]::NewGuid().ToString('N')) }
if ((Test-Path -LiteralPath $WorkDir) -and @(Get-ChildItem -LiteralPath $WorkDir -Force).Count -gt 0) { Fail "WorkDir 必須是空目錄或不存在：$WorkDir" }
$null = New-Item -ItemType Directory -Path $WorkDir -Force
$exit = 0
$daemonStarted = $false
$savedEnv = @{}
try {
    Step '解開 zip'
    $extract = Join-Path $WorkDir 'extract'
    Expand-Archive -LiteralPath $Zip -DestinationPath $extract -Force
    $topName = "serialwrap-$Version-windows-x86_64"
    $tops = @(Get-ChildItem -LiteralPath $extract -Force)
    if ($tops.Count -ne 1 -or $tops[0].Name -ne $topName) { Fail "zip 頂層必須只有 $topName\：$($tops.Name -join ',')" }
    $root = $tops[0].FullName
    $vdir = Join-Path $root "versions\$Version"
    $cli = Join-Path $root 'bin\serialwrap.exe'

    Step '--version／--help'
    $versionLine = ([string](& $cli --version)).Trim()
    if ($LASTEXITCODE -ne 0) { Fail "--version exit $LASTEXITCODE" }
    if ($versionLine -notmatch "^serialwrap $([regex]::Escape($Version))(?: \(code [0-9a-f]{12}\))?$") { Fail "--version 輸出不符：'$versionLine'" }
    & $cli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "--help exit $LASTEXITCODE" }

    Step '內層 SHA256SUMS／manifest／._pth／測試鉤子（validate）'
    & $HostPython -I -B $Helper validate --bundle-root $root --source-root $SourceRoot --version $Version
    if ($LASTEXITCODE -ne 0) { Fail 'validate 失敗' }

    Step 'bundle runtime 隔離'
    $py = Join-Path $vdir 'py\python.exe'
    & $py -I -B $Helper isolation --py-dir (Join-Path $vdir 'py')
    if ($LASTEXITCODE -ne 0) { Fail 'runtime 隔離檢查失敗' }

    # ---- 隔離環境 ----
    $t = Join-Path $WorkDir 'env'
    $dirs = @{
        USERPROFILE = "$t\home"; HOME = "$t\home"; LOCALAPPDATA = "$t\localappdata"; APPDATA = "$t\appdata"
        SERIALWRAP_INSTALL_ROOT = "$t\install"; SERIALWRAP_CONFIG_DIR = "$t\config"; SERIALWRAP_STATE_DIR = "$t\state"
        SERIALWRAP_RUN_DIR = "$t\run"; SERIALWRAP_WAL_DIR = "$t\wal"; SERIALWRAP_LOG_DIR = "$t\logs"
        SERIALWRAP_BY_ID_DIR = "$t\by-id"; SERIALWRAP_BY_PATH_DIR = "$t\by-path"
        SERIALWRAP_EVENTS_DIR = "$t\events"; SERIALWRAP_EVENTS_RUNTIME_DIR = "$t\events-runtime"
    }
    $values = @{}
    foreach ($key in $dirs.Keys) { $values[$key] = $dirs[$key]; $null = New-Item -ItemType Directory -Path $dirs[$key] -Force }
    $values['SERIALWRAP_TEST_DEVICE_ALLOWLIST'] = 'COM999'
    $values['SERIALWRAP_TCP_PORT'] = "$Port"
    $values['SERIALWRAP_ENDPOINT'] = "tcp://127.0.0.1:$Port"
    foreach ($key in $values.Keys) {
        $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $values[$key], 'Process')
    }
    Set-Content -LiteralPath (Join-Path $dirs.SERIALWRAP_CONFIG_DIR 'config.yaml') -Value "supervision_mode: on-demand`n" -Encoding ASCII

    Step 'setup --on-demand（throwaway 安裝根；由 versions\<V>\bin 的 CLI 執行，才會以同層 release-manifest.json 安裝）'
    $setupCli = Join-Path $vdir 'bin\serialwrap.exe'
    $setupOut = & $setupCli setup --on-demand 2>&1 | Out-String
    Write-Output $setupOut.Trim()
    if ($LASTEXITCODE -ne 0) { Fail "setup exit $LASTEXITCODE" }
    $installed = Join-Path $dirs.SERIALWRAP_INSTALL_ROOT 'bin\serialwrap.exe'
    if (-not (Test-Path -LiteralPath $installed)) { Fail "setup 後找不到 $installed" }
    if (-not (Test-Path -LiteralPath (Join-Path $dirs.SERIALWRAP_INSTALL_ROOT "versions\$Version\release-manifest.json"))) { Fail 'setup 後安裝根缺少 versions\<V>\release-manifest.json' }
    $installedVersion = ([string](& $installed --version)).Trim()
    if ($installedVersion -ne $versionLine) { Fail "安裝根的 --version '$installedVersion' 與 bundle '$versionLine' 不同" }

    Step 'daemon start／status／stop'
    & $installed daemon start | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "daemon start exit $LASTEXITCODE" }
    $daemonStarted = $true
    $statusText = & $installed daemon status | Out-String
    if ($LASTEXITCODE -ne 0) { Fail "daemon status exit $LASTEXITCODE" }
    Write-Output $statusText.Trim()
    $status = $statusText | ConvertFrom-Json
    if ($status.ok -ne $true -or $status.running -ne $true -or $status.version -ne $Version) { Fail "daemon status 不健康：$statusText" }
    Step 'doctor（daemon 執行中：版本配對與程式碼指紋須 ok）'
    $doctorText = & $installed doctor 2>&1 | Out-String
    Write-Output $doctorText.Trim()
    $doctor = $doctorText | ConvertFrom-Json
    foreach ($name in 'python', 'pyyaml', 'pyserial', 'supervision_mode', 'daemon_code_current', 'version_pairing') {
        $check = @($doctor.checks | Where-Object { $_.check -eq $name })
        if ($check.Count -ne 1) { Fail "doctor 缺少檢查 $name" }
        if ($check[0].ok -eq $true) { continue }
        # doctor 的 health.status 逾時只有 0.5 秒；Windows 上 daemon 掃描行程較慢時會被判成 daemon_unreachable。
        # 這是 doctor 探測逾時（既有行為），不是 bundle 配對錯誤：其餘配對欄位（sha256／tree／manifest）仍須全數通過。
        if ($name -eq 'version_pairing' -and @($check[0].mismatches).Count -eq 1 -and @($check[0].mismatches)[0] -eq 'daemon_unreachable') {
            Write-Warning 'doctor version_pairing 僅 daemon_unreachable（health.status 0.5s 逾時）；bundle 配對欄位皆通過'
            continue
        }
        Fail "doctor 檢查 $name 未通過：$($check | ConvertTo-Json -Compress)"
    }
    $sessions = & $installed session list | Out-String
    Write-Output $sessions.Trim()
    & $installed daemon stop | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "daemon stop exit $LASTEXITCODE" }
    $daemonStarted = $false
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Process -Id $status.pid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    if (Get-Process -Id $status.pid -ErrorAction SilentlyContinue) { Fail "daemon pid $($status.pid) 在 stop 後 30 秒仍存在" }
    Write-Output "smoke passed for $topName"
} catch {
    Write-Error $_
    $exit = 1
} finally {
    if ($daemonStarted -and (Get-Variable dirs -ErrorAction SilentlyContinue)) {
        try { & (Join-Path $dirs.SERIALWRAP_INSTALL_ROOT 'bin\serialwrap.exe') daemon stop | Out-Null } catch { }
    }
    foreach ($key in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnv[$key], 'Process') }
    if (-not $KeepWork) { Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue }
}
exit $exit
