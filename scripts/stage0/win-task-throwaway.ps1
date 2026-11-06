<#
Stage 0 Windows Task Scheduler 隔離工作建立與清除。
WhatIf 僅輸出計畫 XML 與環境，不建立檔案或呼叫排程器。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [ValidateRange(72, 8760)]
    [int]$Hours = 73,

    [string]$Bundle,

    [string]$AllowCom = "COM24",

    [switch]$Cleanup,

    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($RunId -cnotmatch '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\z') {
    throw "RunId must match YYYYMMDDTHHMMSSZ-xxxxxx exactly."
}
$throwawayAllowedComs = @("COM24")
if ($throwawayAllowedComs -cnotcontains $AllowCom) {
    throw "-AllowCom must be one of the explicitly allowed throwaway COM ports: $($throwawayAllowedComs -join ', ')."
}
if ($Cleanup -and $WhatIf) {
    throw "-Cleanup and -WhatIf cannot be combined."
}
if (-not $Cleanup -and [string]::IsNullOrWhiteSpace($Bundle)) {
    throw "-Bundle is required."
}

$taskName = "serialwrap-throwaway-$RunId"

function ConvertTo-XmlText {
    param([string]$Value)
    $escaped = [System.Security.SecurityElement]::Escape($Value)
    $builder = New-Object System.Text.StringBuilder
    foreach ($character in $escaped.ToCharArray()) {
        $code = [int][char]$character
        if ($code -gt 127) {
            [void]$builder.AppendFormat("&#x{0:X};", $code)
        } else {
            [void]$builder.Append($character)
        }
    }
    return $builder.ToString()
}

function ConvertTo-AsciiEscaped {
    param([string]$Value)
    $builder = New-Object System.Text.StringBuilder
    foreach ($character in $Value.ToCharArray()) {
        $code = [int][char]$character
        if ($code -gt 127) {
            [void]$builder.AppendFormat("\u{0:X4}", $code)
        } else {
            [void]$builder.Append($character)
        }
    }
    return $builder.ToString()
}

function Get-LocalAppDataPath {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return $env:LOCALAPPDATA.TrimEnd('\', '/')
    }
    if ($WhatIf) {
        $userName = if ($env:USERNAME) { $env:USERNAME } elseif ($env:USER) { $env:USER } else { "user" }
        return "C:\Users\$userName\AppData\Local"
    }
    throw "Windows LOCALAPPDATA is not set; refusing to create a scheduled task."
}

$localAppData = Get-LocalAppDataPath
$root = "$localAppData\sw-throwaway\$RunId"
$portSuffix = [Convert]::ToInt32($RunId.Substring($RunId.Length - 6), 16)
$rpcPort = 49152 + ($portSuffix % 16384)
$rpcEndpoint = "tcp://127.0.0.1:$rpcPort"
$environment = [ordered]@{
    SERIALWRAP_STATE_DIR = "$root\state"
    SERIALWRAP_RUN_DIR = "$root\run"
    SERIALWRAP_CONFIG_DIR = "$root\config"
    SERIALWRAP_WAL_DIR = "$root\wal"
    SERIALWRAP_LOG_DIR = "$root\log"
    SERIALWRAP_DATA_DIR = "$root\data"
    SERIALWRAP_PROFILE_DIR = "$root\config\profiles"
    SERIALWRAP_EVENTS_DIR = "$root\events.d"
    SERIALWRAP_EVENTS_RUNTIME_DIR = "$root\events-runtime"
    SERIALWRAP_EVENTS_LOG_PATH = "$root\events-runtime\events.ndjson"
    SERIALWRAP_BY_ID_DIR = "$root\by-id"
    SERIALWRAP_BY_PATH_DIR = "$root\by-path"
    SERIALWRAP_TEST_DEVICE_ALLOWLIST = $AllowCom
    SERIALWRAP_ENDPOINT = $rpcEndpoint
    SERIALWRAP_SOCKET = $rpcEndpoint
}

if ($env:OS -eq "Windows_NT") {
    $principal = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
} elseif ($env:USERDOMAIN -and $env:USERNAME) {
    $principal = "$env:USERDOMAIN\$env:USERNAME"
} elseif ($env:USERNAME) {
    $principal = $env:USERNAME
} elseif ($env:USER) {
    $principal = $env:USER
} else {
    $principal = "CURRENT_USER"
}

$launchPath = "$root\launch.ps1"
$bundlePath = if ([string]::IsNullOrWhiteSpace($Bundle)) { "" } else { $Bundle }
$daemonPath = "$($bundlePath.TrimEnd('\', '/'))\serialwrapd.exe"
$daemonArguments = @(
    "--profile-dir", $environment.SERIALWRAP_PROFILE_DIR,
    "--socket", $environment.SERIALWRAP_SOCKET,
    "--lock", "$($environment.SERIALWRAP_RUN_DIR)\daemon.lock"
)
$encodedPrincipal = ConvertTo-XmlText $principal
$encodedTaskName = ConvertTo-XmlText $taskName
$encodedBundlePath = ConvertTo-XmlText $bundlePath
$escapedActionArgs = [System.String]::Format('-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"', $launchPath)
$escapedActionArgs = ConvertTo-XmlText $escapedActionArgs

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>serialwrap throwaway Stage 0 supervision; configured hours=$Hours; run_id=$RunId.</Description>
    <URI>\$encodedTaskName</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$encodedPrincipal</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$encodedPrincipal</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$escapedActionArgs</Arguments>
      <WorkingDirectory>$encodedBundlePath</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($WhatIf) {
    Write-Output $taskXml
    foreach ($key in ($environment.Keys | Sort-Object)) {
        Write-Output "$key=$(ConvertTo-AsciiEscaped ([string]$environment[$key]))"
    }
    return
}

if ($env:OS -ne "Windows_NT") {
    throw "Scheduled tasks can only be created or removed on a Windows host."
}

if ($Cleanup) {
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask -and $existingTask.State -eq "Running") {
        Stop-ScheduledTask -TaskName $taskName
    }
    if ($null -ne $existingTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    $lockPath = "$root\run\daemon.lock"
    $stopDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ($true) {
        $throwawayProcesses = @(
            Get-CimInstance -ClassName Win32_Process -Filter "Name='serialwrapd.exe'" -ErrorAction Stop |
                Where-Object {
                    $_.CommandLine -and
                    $_.CommandLine.IndexOf($lockPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
                }
        )
        if ($throwawayProcesses.Count -eq 0) {
            break
        }
        if ([DateTime]::UtcNow -ge $stopDeadline) {
            foreach ($process in $throwawayProcesses) {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            }
            Start-Sleep -Milliseconds 250
        } else {
            Start-Sleep -Milliseconds 250
        }
        if ([DateTime]::UtcNow -ge $stopDeadline.AddSeconds(5)) {
            throw "Throwaway serialwrapd.exe process remains after cleanup."
        }
    }
    Write-Output "No throwaway daemon process remains: $taskName"
    Write-Output "Removed throwaway task: $taskName"
    return
}

if ([string]::IsNullOrWhiteSpace($Bundle) -or -not [System.IO.Path]::IsPathRooted($Bundle)) {
    throw "-Bundle must be an absolute Windows path."
}
if (-not (Test-Path -LiteralPath $Bundle -PathType Container)) {
    throw "Bundle directory does not exist: $(ConvertTo-AsciiEscaped $Bundle)"
}
if (-not (Test-Path -LiteralPath $daemonPath -PathType Leaf)) {
    throw "Bundle is missing serialwrapd.exe: $(ConvertTo-AsciiEscaped $daemonPath)"
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Throwaway task already exists; refusing to overwrite: $taskName"
}
if (Test-Path -LiteralPath $root) {
    throw "Throwaway root already exists; refusing to overwrite: $(ConvertTo-AsciiEscaped $root)"
}

$paths = @(
    $environment.SERIALWRAP_STATE_DIR,
    $environment.SERIALWRAP_RUN_DIR,
    $environment.SERIALWRAP_CONFIG_DIR,
    $environment.SERIALWRAP_PROFILE_DIR,
    $environment.SERIALWRAP_WAL_DIR,
    $environment.SERIALWRAP_LOG_DIR,
    $environment.SERIALWRAP_DATA_DIR,
    $environment.SERIALWRAP_EVENTS_DIR,
    $environment.SERIALWRAP_EVENTS_RUNTIME_DIR,
    $environment.SERIALWRAP_BY_ID_DIR,
    $environment.SERIALWRAP_BY_PATH_DIR
)
foreach ($path in $paths) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}

$launchSettings = [ordered]@{
    daemon_path = $daemonPath
    daemon_args = $daemonArguments
    hours = $Hours
    environment = $environment
}
$launchJson = ConvertTo-Json -InputObject $launchSettings -Depth 5 -Compress
$utf8Bom = New-Object System.Text.UTF8Encoding -ArgumentList $true
[System.IO.File]::WriteAllText("$root\launch-settings.json", $launchJson, $utf8Bom)
$launchBytes = [System.Text.Encoding]::UTF8.GetBytes($launchJson)
$launchData = [Convert]::ToBase64String($launchBytes)
$launcher = @'
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$settingsJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("__LAUNCH_DATA__"))
$settings = $settingsJson | ConvertFrom-Json

# Task Scheduler 會繼承使用者層級環境；先移除所有產品變數，再套用本次 throwaway 設定。
Get-ChildItem Env: | Where-Object { $_.Name.StartsWith("SERIALWRAP_", [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" -ErrorAction Stop }
foreach ($property in $settings.environment.PSObject.Properties) {
    [System.Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, "Process")
}

function Quote-ProcessArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

$argumentLine = (@($settings.daemon_args | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " ")
$logToken = [Guid]::NewGuid().ToString("N")
$stdoutPath = "$env:SERIALWRAP_LOG_DIR\serialwrapd.$logToken.out.log"
$stderrPath = "$env:SERIALWRAP_LOG_DIR\serialwrapd.$logToken.err.log"
$daemon = Start-Process -FilePath $settings.daemon_path -ArgumentList $argumentLine -NoNewWindow -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
$deadline = [DateTime]::UtcNow.AddHours([double]$settings.hours)

while ($true) {
    $daemon.Refresh()
    if ($daemon.HasExited) {
        if ([DateTime]::UtcNow -lt $deadline -and $daemon.ExitCode -eq 0) {
            exit 1
        }
        exit $daemon.ExitCode
    }
    if ([DateTime]::UtcNow -ge $deadline) {
        Stop-Process -Id $daemon.Id -Force
        $daemon.WaitForExit()
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
'@
$launcher = $launcher.Replace("__LAUNCH_DATA__", $launchData)
[System.IO.File]::WriteAllText($launchPath, $launcher, $utf8Bom)

try {
    Register-ScheduledTask -TaskName $taskName -Xml $taskXml | Out-Null
} catch {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    throw
}

Write-Output "Registered throwaway task: $taskName"
Write-Output "Isolated root: $(ConvertTo-AsciiEscaped $root)"
Write-Output "RPC endpoint: $rpcEndpoint"
