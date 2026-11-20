#requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$TestSpecs = @(
    [pscustomobject]@{
        Nodeid = 'cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c'
        Crate = 'sw-pal'
        Target = 'exec_delegate'
        TestName = 'cap_interactive_desktop__exec_or_wait_ctrl_c'
    }
    [pscustomobject]@{
        Nodeid = 'cargo:sw-pal::lock::cap_interactive_desktop__ctrl_break_stops_wait'
        Crate = 'sw-pal'
        Target = 'lock'
        TestName = 'cap_interactive_desktop__ctrl_break_stops_wait'
    }
    [pscustomobject]@{
        Nodeid = 'cargo:sw-daemon::daemon_process::cap_interactive_desktop__windows_console_ctrl_triggers_orderly_stop'
        Crate = 'sw-daemon'
        Target = 'daemon_process'
        TestName = 'cap_interactive_desktop__windows_console_ctrl_triggers_orderly_stop'
    }
)

$TargetTriple = 'x86_64-pc-windows-gnu'
$BuildArgs = @(
    'test', '--no-run', '--message-format=json', '--target', $TargetTriple,
    '--package', 'sw-pal', '--package', 'sw-daemon', '--features', 'test-fakes',
    '--test', 'exec_delegate', '--test', 'lock', '--test', 'daemon_process'
)
$ReportArgv = @(
    '--package', 'sw-pal', '--package', 'sw-core', '--package', 'sw-daemon',
    '--features', 'test-fakes', '--', '--ignored', 'cap_interactive_desktop__'
)

function Quote-Bash([string]$Value) {
    if ($Value.Contains("'")) {
        throw '工作樹路徑含單引號，拒絕組合 WSL shell 命令。'
    }
    return "'$Value'"
}

function Invoke-Wsl([string]$Command) {
    $output = & wsl.exe -d $script:WslDistro --exec bash -lc $Command
    $code = $LASTEXITCODE
    $text = (@($output) | ForEach-Object { [string]$_ }) -join "`n"
    if ($code -ne 0) {
        throw "WSL 命令失敗，exit=$code：$Command`n$text"
    }
    return $text
}

function Get-GitState {
    $root = Quote-Bash $script:RepoLinux
    $head = (Invoke-Wsl "cd $root && git rev-parse HEAD").Trim()
    $status = Invoke-Wsl "cd $root && git status --porcelain --untracked-files=normal -- . ':(exclude)target'"
    return [pscustomobject][ordered]@{
        head = $head
        clean = [string]::IsNullOrWhiteSpace($status)
    }
}

function Get-TextSha256([string]$Text) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    return [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Get-WindowsPath([string]$LinuxPath) {
    return (Invoke-Wsl ("wslpath -w " + (Quote-Bash $LinuxPath))).Trim()
}

if ([string]::IsNullOrWhiteSpace($env:WT_SESSION) -or
    [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
    throw '請在 Windows Terminal 的互動 PowerShell 分頁直接執行；不可從 WSL interop、排程工作或管線啟動。'
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).ProviderPath
if ($RepoRoot -notmatch '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\') {
    throw "工作樹必須從 WSL UNC 路徑開啟：$RepoRoot"
}
$script:WslDistro = $Matches[1]
$script:RepoLinux = (& wsl.exe -d $script:WslDistro --exec wslpath -u $RepoRoot | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($script:RepoLinux)) {
    throw '無法從 Windows UNC 路徑解析工作樹的 WSL 路徑。'
}

$RunId = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + "-$PID"
$OutputDirectory = Join-Path $RepoRoot "target\win-interactive-l1\$RunId"
$ScratchDirectory = Join-Path $env:LOCALAPPDATA "Temp\sw-interactive-l1\$RunId"
$null = New-Item -ItemType Directory -Force -Path $OutputDirectory
$null = New-Item -ItemType Directory -Force -Path $ScratchDirectory

$GitStart = $null
$GitEnd = $null
$Toolchain = [ordered]@{ rustc_vv = $null; rustc_vv_sha256 = $null; error = $null }
$Rustflags = [ordered]@{
    RUSTFLAGS = [ordered]@{ set = $false; sha256 = $null }
    CARGO_ENCODED_RUSTFLAGS = [ordered]@{ set = $false; sha256 = $null }
}
$ExecutableRecords = [System.Collections.Generic.List[object]]::new()
$Rows = [System.Collections.Generic.List[object]]::new()
$ExitCode = 1
$ExecutionComplete = $false
$AllTestsExecuted = $true
$FailureReason = $null

try {
    $GitStart = Get-GitState
    if ($GitStart.head -notmatch '^[0-9a-f]{40}$') {
        throw "git HEAD 無效：$($GitStart.head)"
    }
    if (-not $GitStart.clean) {
        throw '工作樹不乾淨（target/ 除外）；請先提交候選版，再產生 Windows provenance。'
    }

    $root = Quote-Bash $script:RepoLinux
    $target = Quote-Bash "$($script:RepoLinux)/target"
    $availableD = Invoke-Wsl ("cd $root && set -o pipefail && df -BG --output=avail /mnt/d | tail -n 1")
    $availableDigits = $availableD.Trim() -replace '[^0-9]', ''
    if ($availableDigits -notmatch '^\d+$' -or [int64]$availableDigits -lt 100) {
        throw "D 槽可用空間不足 100 GB 或無法判定（df 回報：$availableD）；停止建置。"
    }
    $buildArgsQuoted = @($BuildArgs | ForEach-Object { Quote-Bash $_ }) -join ' '
    $buildCommand = 'cd ' + $root + ' && export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)" CARGO_TARGET_DIR=' + $target +
        ' CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc' +
        ' && heavy-slot serialwrap-gfxc nice -n 10 ionice -c2 -n7 cargo ' + $buildArgsQuoted
    $buildOutput = Invoke-Wsl $buildCommand
    $artifactPaths = @{}
    foreach ($line in ($buildOutput -split "`n")) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith('{')) { continue }
        try {
            $record = $trimmed | ConvertFrom-Json -AsHashtable
        } catch {
            continue
        }
        if ($record.reason -ne 'compiler-artifact' -or $record.profile.test -ne $true) { continue }
        $targetName = [string]$record.target.name
        if ($targetName -notin @('exec_delegate', 'lock', 'daemon_process') -or -not $record.executable) { continue }
        if ($artifactPaths.ContainsKey($targetName)) {
            throw "Cargo 回報重複測試執行檔：$targetName"
        }
        $artifactPaths[$targetName] = [string]$record.executable
    }
    foreach ($targetName in @('exec_delegate', 'lock', 'daemon_process')) {
        if (-not $artifactPaths.ContainsKey($targetName)) {
            throw "Cargo 未建置必要測試執行檔：$targetName"
        }
    }

    $rustcVv = Invoke-Wsl "cd $root && rustc -vV"
    $Toolchain = [ordered]@{
        rustc_vv = "$rustcVv`n"
        rustc_vv_sha256 = Get-TextSha256 "$rustcVv`n"
        error = $null
    }
    $flagsCode = 'import os,json,hashlib; names=("RUSTFLAGS","CARGO_ENCODED_RUSTFLAGS"); print(json.dumps({n:{"set":n in os.environ,"sha256":hashlib.sha256(os.environ[n].encode()).hexdigest() if n in os.environ else None} for n in names},sort_keys=True))'
    $flagsJson = Invoke-Wsl ("cd $root && python3 -c " + (Quote-Bash $flagsCode))
    $Rustflags = $flagsJson | ConvertFrom-Json -AsHashtable

    foreach ($spec in $TestSpecs) {
        $linuxExe = $artifactPaths[$spec.Target]
        $sourceExe = Get-WindowsPath $linuxExe
        $localExe = Join-Path $ScratchDirectory "$($spec.Target).exe"
        Copy-Item -LiteralPath $sourceExe -Destination $localExe -Force
        $exeHash = (Get-FileHash -LiteralPath $localExe -Algorithm SHA256).Hash.ToLowerInvariant()
        $sourceHash = (Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($exeHash -ne $sourceHash) {
            throw "測試執行檔複製後 SHA256 不符：$($spec.Target)"
        }
        if (-not ($ExecutableRecords | Where-Object { $_.target -eq $spec.Target })) {
            $nodeids = @($TestSpecs | Where-Object { $_.Target -eq $spec.Target } | ForEach-Object { $_.Nodeid })
            $ExecutableRecords.Add([ordered]@{
                crate = $spec.Crate
                target = $spec.Target
                path = $localExe
                sha256 = $exeHash
                nodeids = $nodeids
            })
        }

        $listed = @(& $localExe --ignored --list --format terse)
        $listExit = $LASTEXITCODE
        $expectedListing = "$($spec.TestName): test"
        if ($listExit -ne 0 -or $listed -notcontains $expectedListing) {
            $AllTestsExecuted = $false
            $Rows.Add([ordered]@{
                nodeid = $spec.Nodeid
                outcome = 'error'
                skip_kind = $null
                phases = [ordered]@{ setup = 'failed'; call = $null; teardown = $null }
            })
            continue
        }

        $previousDiag = $env:SW_PAL_CTRLC_DIAG_DIR
        if ($spec.Target -eq 'exec_delegate') {
            $env:SW_PAL_CTRLC_DIAG_DIR = $ScratchDirectory
        }
        try {
            Push-Location $ScratchDirectory
            & $localExe --ignored --exact $spec.TestName --nocapture --test-threads=1
            $testExit = $LASTEXITCODE
        } catch {
            $testExit = 127
            $AllTestsExecuted = $false
            Write-Host "測試啟動失敗：$($spec.Nodeid)：$($_.Exception.Message)" -ForegroundColor Red
        } finally {
            if ((Get-Location).Path -eq $ScratchDirectory) { Pop-Location }
            if ($spec.Target -eq 'exec_delegate') {
                if ($null -eq $previousDiag) { Remove-Item Env:SW_PAL_CTRLC_DIAG_DIR -ErrorAction SilentlyContinue }
                else { $env:SW_PAL_CTRLC_DIAG_DIR = $previousDiag }
            }
        }
        $outcome = if ($testExit -eq 0) { 'passed' } else { 'failed' }
        $Rows.Add([ordered]@{
            nodeid = $spec.Nodeid
            outcome = $outcome
            skip_kind = $null
            phases = [ordered]@{ setup = 'passed'; call = $outcome; teardown = 'passed' }
        })
    }
    $ExecutionComplete = $Rows.Count -eq $TestSpecs.Count -and $AllTestsExecuted
    $ExitCode = if ($ExecutionComplete -and @($Rows | Where-Object { $_.outcome -ne 'passed' }).Count -eq 0) { 0 } else { 1 }
} catch {
    $AllTestsExecuted = $false
    $FailureReason = $_.Exception.Message
    Write-Host "OD-89 harness 失敗：$FailureReason" -ForegroundColor Red
    $ExitCode = 1
} finally {
    try { $GitEnd = Get-GitState } catch {
        $GitEnd = [pscustomobject][ordered]@{ head = $null; clean = $false }
    }
    $hashesStable = $true
    foreach ($executable in $ExecutableRecords) {
        if (-not (Test-Path -LiteralPath $executable.path) -or
            (Get-FileHash -LiteralPath $executable.path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $executable.sha256) {
            $hashesStable = $false
        }
    }
    $finished = $ExecutionComplete -and $GitStart -and $GitStart.clean -and $GitEnd.clean -and
        $GitStart.head -eq $GitEnd.head -and $hashesStable
    $allResults = @($Rows)
    $counts = [ordered]@{
        passed = @($allResults | Where-Object { $_.outcome -eq 'passed' }).Count
        failed = @($allResults | Where-Object { $_.outcome -in @('failed', 'error') }).Count
        skipped = @($allResults | Where-Object { $_.outcome -eq 'skipped' }).Count
    }
    $gitStartHead = if ($GitStart) { $GitStart.head } else { $null }
    $gitStartClean = if ($GitStart) { $GitStart.clean } else { $false }
    $gitEndHead = if ($GitEnd) { $GitEnd.head } else { $null }
    $gitEndClean = if ($GitEnd) { $GitEnd.clean } else { $false }
    $Provenance = [ordered]@{
        schema = 'cargo-l1-provenance.v1'
        platform = 'windows'
        build_target = $TargetTriple
        git = [ordered]@{
            head = $gitStartHead
            clean = $gitStartClean
            end = [ordered]@{ head = $gitEndHead; clean = $gitEndClean }
        }
        toolchain = $Toolchain
        rustflags = $Rustflags
        executables = @($ExecutableRecords)
    }
    $L1Report = [ordered]@{
        schema_version = 2
        format = 'l1-results.v1'
        runner = 'cargo'
        platform = 'windows'
        argv = $ReportArgv
        markexpr = ''
        counts = [ordered]@{ all = $counts }
        resources = [ordered]@{}
        uart_kind = 'native'
        not_collected_platform = @()
        collected = @($TestSpecs | ForEach-Object { $_.Nodeid })
        deselected = @()
        results = $allResults
        exit_code = $ExitCode
        finished = [bool]$finished
        core_portable_platform_skips = @()
        provenance = $Provenance
        deployed_content = $null
    }
    if ($FailureReason) { $L1Report.failure_reason = $FailureReason }
    $reportPath = Join-Path $OutputDirectory 'l1-results.json'
    $json = $L1Report | ConvertTo-Json -Depth 32
    [System.IO.File]::WriteAllText($reportPath, "$json`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host "l1-results.v1：$reportPath"
    Write-Host "collected=$($L1Report.collected.Count) passed=$($counts.passed) failed=$($counts.failed) finished=$($L1Report.finished)"
}

exit $ExitCode
