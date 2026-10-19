param(
    [Parameter(Mandatory = $true)][string]$RustExe,
    [Parameter(Mandatory = $true)][string]$RepoSnapshot,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$Python3123,
    [string]$GitRevision,
    [string]$ContractRevision,
    [string]$ComPair,
    [string]$Label = 'rust-1a-win'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-Json([string]$Path, [object]$Value) {
    $parent = Split-Path -Parent $Path
    $null = New-Item -ItemType Directory -Path $parent -Force
    $json = ConvertTo-Json -InputObject $Value -Depth 16
    [System.IO.File]::WriteAllText($Path, $json + "`n", [System.Text.UTF8Encoding]::new($false))
}

function Copy-RepoTree([string]$Source, [string]$Destination) {
    $null = New-Item -ItemType Directory -Path $Destination -Force
    $sourceRoot = (Resolve-Path -LiteralPath $Source).ProviderPath.TrimEnd('\', '/')
    $items = Get-ChildItem -LiteralPath $sourceRoot -Force -Recurse
    foreach ($item in $items) {
        $relative = $item.FullName.Substring($sourceRoot.Length).TrimStart('\', '/')
        $skip = $false
        foreach ($part in ($relative -split '[\\/]')) {
            if ($part -in @('__pycache__', '.pytest_cache', 'target')) { $skip = $true; break }
        }
        if ($skip) { continue }
        $target = Join-Path $Destination $relative
        if ($item.PSIsContainer) {
            $null = New-Item -ItemType Directory -Path $target -Force
        } else {
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Copy-RepoPath([string]$RepoRoot, [string]$RelativePath, [string]$DestinationRoot, [bool]$Required) {
    $source = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source)) {
        if ($Required) { throw "repo snapshot 缺少必要路徑：$RelativePath" }
        return $false
    }
    $destination = Join-Path $DestinationRoot $RelativePath
    if (Test-Path -LiteralPath $source -PathType Container) {
        Copy-RepoTree $source $destination
    } else {
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    return $true
}

function Get-LoopbackListenerSnapshot() {
    if (-not (Get-Command 'Get-NetTCPConnection' -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ ok = $false; listeners = @(); error = 'Get-NetTCPConnection 不可用' }
    }
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1' } |
            ForEach-Object {
            '{0}|{1}|{2}' -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess
        } | Sort-Object -Unique)
        return [pscustomobject]@{ ok = $true; listeners = $listeners; error = $null }
    } catch {
        return [pscustomobject]@{ ok = $false; listeners = @(); error = $_.Exception.Message }
    }
}

function Confirm-BlackboxRelease([string]$StagingRepo, [string]$ScratchRoot, [object]$BaselineListeners) {
    $errors = [System.Collections.Generic.List[string]]::new()
    $remaining = @()
    $newListeners = @()
    $started = [DateTime]::UtcNow
    if (-not $BaselineListeners.ok) {
        $errors.Add("無法取得 A 輪前的 listener 快照：$($BaselineListeners.error)")
        return [ordered]@{ ok = $false; waited_ms = 0; remaining_processes = @(); new_listeners = @(); errors = @($errors) }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        try {
            $processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
        } catch {
            $errors.Add("無法查詢 Windows 行程：$($_.Exception.Message)")
            break
        }
        $remaining = @($processes | Where-Object {
            $_.ProcessId -ne $PID -and $_.CommandLine -and
            (($_.CommandLine.IndexOf($StagingRepo, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -or
             ($_.CommandLine.IndexOf($ScratchRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0))
        })

        $listenerSnapshot = Get-LoopbackListenerSnapshot
        if (-not $listenerSnapshot.ok) {
            $errors.Add("無法取得 A 輪後的 listener 快照：$($listenerSnapshot.error)")
            break
        }
        $baseline = @($BaselineListeners.listeners)
        $newListeners = @($listenerSnapshot.listeners | Where-Object { $_ -notin $baseline })
        if ($remaining.Count -eq 0 -and $newListeners.Count -eq 0 -and $errors.Count -eq 0) { break }
        if ([DateTime]::UtcNow -ge $deadline) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)

    $processRows = @($remaining | ForEach-Object {
        [ordered]@{ pid = [int]$_.ProcessId; name = [string]$_.Name }
    })
    $elapsed = [int]([DateTime]::UtcNow - $started).TotalMilliseconds
    return [ordered]@{
        ok = ($remaining.Count -eq 0 -and $newListeners.Count -eq 0 -and $errors.Count -eq 0)
        waited_ms = $elapsed
        remaining_processes = $processRows
        new_listeners = @($newListeners)
        errors = @($errors)
    }
}

function Get-VerifiedPython3123([string]$CandidateDirectory, [string]$RepoRoot, [string]$ScratchRoot) {
    $checksumFile = Join-Path $RepoRoot 'scripts/rust/win-python-3.12.3.sha256'
    if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) { return $null }
    $expected = ((Get-Content -LiteralPath $checksumFile -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
    $archiveName = 'python-3.12.3-embed-amd64.zip'
    $archive = Join-Path $ScratchRoot $archiveName
    $candidateArchive = $null
    if ($CandidateDirectory) {
        if (Test-Path -LiteralPath $CandidateDirectory -PathType Leaf) {
            $candidateArchive = $CandidateDirectory
        } else {
            $candidatePython = Join-Path $CandidateDirectory 'python.exe'
            if (Test-Path -LiteralPath $candidatePython -PathType Leaf) {
                $runtimeRoot = Join-Path $ScratchRoot 'python-3.12.3'
                Copy-RepoTree $CandidateDirectory $runtimeRoot
                $pythonExe = Join-Path $runtimeRoot 'python.exe'
                $version = & $pythonExe -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
                $versionLine = [string]($version | Select-Object -Last 1)
                if ($LASTEXITCODE -ne 0 -or $versionLine.Trim() -ne '3.12.3') {
                    throw "指定的 CPython 執行目錄版本不是 3.12.3：$CandidateDirectory"
                }
                return $runtimeRoot
            }
            $candidateArchive = Join-Path $CandidateDirectory $archiveName
        }
    }
    $usingCandidate = $candidateArchive -and (Test-Path -LiteralPath $candidateArchive -PathType Leaf)
    try {
        if ($usingCandidate) {
            Copy-Item -LiteralPath $candidateArchive -Destination $archive -Force
        } else {
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.3/$archiveName" `
                -OutFile $archive -UseBasicParsing
        }
    } catch {
        if ($usingCandidate) { throw "指定 CPython embeddable 無法讀取：$candidateArchive；$($_.Exception.Message)" }
        return $null
    }
    if ((Get-Sha256 $archive) -ne $expected) {
        if ($usingCandidate) { throw "指定 CPython embeddable SHA-256 不符：$candidateArchive" }
        throw 'python.org CPython 3.12.3 embeddable SHA-256 不符'
    }
    $runtimeRoot = Join-Path $ScratchRoot 'python-3.12.3'
    $null = New-Item -ItemType Directory -Path $runtimeRoot -Force
    Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force
    $pythonExe = Join-Path $runtimeRoot 'python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { return $null }
    $version = & $pythonExe -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    $versionLine = [string]($version | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or $versionLine.Trim() -ne '3.12.3') { return $null }
    return $runtimeRoot
}

function Enable-EmbeddedSitePackages([string]$RuntimeRoot, [string]$RepoRoot) {
    $sitePackages = & py -3.12 -c "import sysconfig; print(sysconfig.get_path('purelib'))"
    if ($LASTEXITCODE -ne 0 -or -not $sitePackages -or -not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        return $false
    }
    $pth = Get-ChildItem -LiteralPath $RuntimeRoot -Filter 'python*._pth' | Select-Object -First 1
    if (-not $pth) { return $false }
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in Get-Content -LiteralPath $pth.FullName) {
        if ($line.Trim() -and $line.Trim() -ne 'import site') { $lines.Add($line.Trim()) }
    }
    $lines.Add($RepoRoot)
    $sitePackagesPath = [string]($sitePackages | Select-Object -Last 1)
    $lines.Add($sitePackagesPath.Trim())
    $lines.Add('import site')
    [System.IO.File]::WriteAllLines($pth.FullName, $lines, [System.Text.UTF8Encoding]::new($false))
    & (Join-Path $RuntimeRoot 'python.exe') -I -c 'import yaml'
    return $LASTEXITCODE -eq 0
}

function New-WindowsDevBundle([string]$BundleRoot, [string]$RustExecutable, [string]$RepoRoot,
                              [string]$RuntimeRoot, [string]$RuntimeExecutable,
                              [string]$Revision, [string]$ContractsRevision) {
    $version = (Get-Content -LiteralPath (Join-Path $RepoRoot 'VERSION') -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION 格式不合法：$version" }
    $versionRoot = Join-Path $BundleRoot "versions\$version"
    $binRoot = Join-Path $BundleRoot 'bin'
    $versionBinRoot = Join-Path $versionRoot 'bin'
    $pythonRoot = Join-Path $versionRoot 'py'
    $null = New-Item -ItemType Directory -Path $binRoot, $versionBinRoot, $pythonRoot -Force
    Copy-Item -LiteralPath $RustExecutable -Destination (Join-Path $binRoot 'serialwrap.exe') -Force
    Copy-Item -LiteralPath $RustExecutable -Destination (Join-Path $versionBinRoot 'serialwrap.exe') -Force
    Copy-RepoTree $RuntimeRoot $pythonRoot

    $pythonRelative = if (Test-Path -LiteralPath (Join-Path $pythonRoot 'python.exe')) {
        'py\python.exe'
    } else {
        'py\Scripts\python.exe'
    }
    $runtimeExe = Join-Path $pythonRoot $pythonRelative.Substring(3)
    $runtimeHash = Get-Sha256 $runtimeExe
    $cliHash = Get-Sha256 (Join-Path $versionBinRoot 'serialwrap.exe')
    $launcherPath = Join-Path $RepoRoot 'serialwrapd.py'
    $skillPath = Join-Path $RepoRoot 'sw_core/assets/skill/SKILL.md'
    $windowsSkillPath = Join-Path $RepoRoot 'sw_core/assets/skill/SKILL_WINDOWS.md'
    $profilesPath = Join-Path $RepoRoot 'sw_core/assets/profiles'
    $supervisorPath = Join-Path $RepoRoot 'sw_core/systemd_units.py'
    $runtimeVersion = & $runtimeExe -I -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE -ne 0) { throw '無法查詢 bundle Python 版本' }
    $packageTree = & $runtimeExe -I -c "import sw_core.release_manifest as m; print(m.package_tree_sha256())"
    if ($LASTEXITCODE -ne 0) { throw '無法計算 Python delegate 套件 tree hash' }
    $profileTree = & $runtimeExe -I -c "import sw_core.release_manifest as m, sys; print(m.tree_sha256(sys.argv[1]))" $profilesPath
    if ($LASTEXITCODE -ne 0) { throw '無法計算 profiles tree hash' }
    $packageTreeHash = [string]($packageTree | Select-Object -Last 1)
    $profileTreeHash = [string]($profileTree | Select-Object -Last 1)
    if ($packageTreeHash.Trim() -notmatch '^[0-9a-f]{64}$') { throw 'Python delegate package tree hash 格式不合法' }
    if ($profileTreeHash.Trim() -notmatch '^[0-9a-f]{64}$') { throw 'profiles tree hash 格式不合法' }
    $entry = @($pythonRelative, '-I', '-B', '-m', 'sw_core.cli')
    $manifest = [ordered]@{
        schema_version = 1
        bundle_version = $version
        stage = '1A'
        created_at = [DateTime]::UtcNow.ToString('o')
        target = 'x86_64-pc-windows-gnu'
        git_revision = $Revision
        contract_revision = $ContractsRevision
        components = [ordered]@{
            cli = [ordered]@{
                implementation = 'rust'
                version = $version
                git_revision = $Revision
                path = 'bin/serialwrap.exe'
                sha256 = $cliHash
            }
            daemon = [ordered]@{
                implementation = 'python'
                version = $version
                git_revision = $Revision
                launcher = [ordered]@{ path = $launcherPath; sha256 = Get-Sha256 $launcherPath }
                entry = @($pythonRelative, '-I', '-B', $launcherPath)
                runtime = [ordered]@{ path = $pythonRelative.Replace('\', '/'); python_version = ($runtimeVersion | Select-Object -Last 1).Trim(); sha256 = $runtimeHash }
                package = [ordered]@{ name = 'serialwrap'; version = $version; wheel_sha256 = $null; tree_sha256 = $packageTreeHash.Trim() }
            }
            python_cli_delegate = [ordered]@{ entry = $entry; tree_sha256 = $packageTreeHash.Trim(); runtime_python_version = ($runtimeVersion | Select-Object -Last 1).Trim() }
            assets = [ordered]@{
                skill_sha256 = Get-Sha256 $skillPath
                skill_windows_sha256 = Get-Sha256 $windowsSkillPath
                profiles_sha256 = $profileTreeHash.Trim()
            }
            supervisor = [ordered]@{ default_mode = 'on-demand'; definition_sha256 = Get-Sha256 $supervisorPath }
        }
        pairing = [ordered]@{ rule = 'exact'; cli_version = $version; daemon_version = $version }
    }
    Write-Json (Join-Path $versionRoot 'release-manifest.json') $manifest
    Write-Json (Join-Path $binRoot 'current-release.json') ([ordered]@{
        bundle_version = $version
        dir = "versions\$version"
    })
    return $BundleRoot
}

function Invoke-Blackbox([string]$Name, [string]$JunitPath, [string]$CliPath, [string]$DaemonPath,
                         [string]$RuntimePath, [string]$RunLabel, [bool]$RustRun,
                         [string]$L1Report, [bool]$RuntimeMissing, [string]$BundlePath) {
    $env:SERIALWRAP_CLI_EXE = $CliPath
    $env:SERIALWRAP_DAEMON_EXE = $DaemonPath
    $env:SERIALWRAP_AB_LABEL = $RunLabel
    if ($RuntimePath) { $env:SW_BASELINE_PYTHON = $RuntimePath } else { Remove-Item Env:SW_BASELINE_PYTHON -ErrorAction SilentlyContinue }
    if ($BundlePath) { $env:SW_WIN_DEV_BUNDLE = $BundlePath }
    if ($L1Report -and (Test-Path -LiteralPath (Join-Path $script:stagingRepo 'tests/resource_report_plugin.py'))) {
        $env:SW_RESOURCE_REPORT = $L1Report
    } else {
        Remove-Item Env:SW_RESOURCE_REPORT -ErrorAction SilentlyContinue
    }
    $arguments = @('-3.12', '-m', 'pytest', 'tests/contract_blackbox', '--confcutdir=tests/contract_blackbox',
                   '-p', 'no:cacheprovider', "--junitxml=$JunitPath")
    if ($RustRun) { $arguments += @('-m', 'core_portable or windows_native') }
    if ($RuntimeMissing) { $arguments += @('-k', 'not test_bb_cli_help and not test_bb_delegation') }
    $stdoutPath = Join-Path $OutDir "$Name.stdout.log"
    $stderrPath = Join-Path $OutDir "$Name.stderr.log"
    $oldErrorActionPreference = $ErrorActionPreference
    Push-Location -LiteralPath $script:stagingRepo
    try {
        # Windows PowerShell 5.1 將 native stderr 轉成 ErrorRecord；Continue 才能保存 pytest 原始退出碼。
        $ErrorActionPreference = 'Continue'
        & py @arguments 1> $stdoutPath 2> $stderrPath
        $pytestExit = [int]$LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $oldErrorActionPreference
    }
    return [ordered]@{
        exit = $pytestExit
        junit = $JunitPath
        stdout = $stdoutPath
        stderr = $stderrPath
        l1_report = if ($L1Report -and (Test-Path -LiteralPath $L1Report)) { $L1Report } else { $null }
    }
}

$sourceRoot = (Resolve-Path -LiteralPath $RepoSnapshot).ProviderPath
$rustExecutable = (Resolve-Path -LiteralPath $RustExe).ProviderPath
if (-not (Test-Path -LiteralPath $rustExecutable -PathType Leaf)) { throw "Rust CLI 不存在：$RustExe" }
$outputRoot = [System.IO.Path]::GetFullPath($OutDir)
$null = New-Item -ItemType Directory -Path $outputRoot -Force
$runId = '{0}-{1}' -f [DateTime]::UtcNow.ToString('yyMMdd-HHmmss'), $PID
$localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [Environment]::GetFolderPath('LocalApplicationData') }
if (-not $localAppData) { throw 'Windows LOCALAPPDATA 不可用' }
$scratchRoot = Join-Path $localAppData "Temp\sw-rust-test\$runId"
$stagingRepo = Join-Path $scratchRoot 'repo'
$bundleRoot = Join-Path $scratchRoot 'bundle'
$null = New-Item -ItemType Directory -Path $stagingRepo, $bundleRoot -Force
$missing = [System.Collections.Generic.List[string]]::new()

try {
    $requiredPaths = @('serialwrap.py', 'serialwrapd.py', 'sw_core', 'tests/contract_blackbox',
                       'tests/ab_env.py', 'VERSION', 'contracts')
    foreach ($relative in $requiredPaths) { $null = Copy-RepoPath $sourceRoot $relative $stagingRepo $true }
    foreach ($relative in @('tests/resource_report.py', 'tests/resource_report_plugin.py',
                            'realhw/deployed_content.py')) {
        if (-not (Copy-RepoPath $sourceRoot $relative $stagingRepo $false)) { $missing.Add($relative) }
    }

    $revision = $GitRevision
    $contractsRevision = $ContractRevision
    if (-not $revision -or $revision -notmatch '^[0-9a-f]{40}$') {
        $revision = (& git -C $sourceRoot rev-parse HEAD 2>$null | Select-Object -Last 1).Trim()
    }
    if (-not $contractsRevision -or $contractsRevision -notmatch '^[0-9a-f]{40}$') {
        $contractsRevision = (& git -C $sourceRoot rev-parse 'HEAD:contracts' 2>$null | Select-Object -Last 1).Trim()
    }
    if ($revision -notmatch '^[0-9a-f]{40}$' -or $contractsRevision -notmatch '^[0-9a-f]{40}$') {
        throw '無法取得 Git 與 contracts revision，拒絕產生不完整 dev bundle'
    }

    $runtimeExe = $null
    $runtimeRoot = Get-VerifiedPython3123 $Python3123 $sourceRoot $scratchRoot
    $runtimeMissing = $null -eq $runtimeRoot
    if ($runtimeMissing) { $missing.Add('CPython 3.12.3 embeddable 方言基線不存在或無法驗證') }
    if (-not $runtimeMissing) {
        if (-not (Enable-EmbeddedSitePackages $runtimeRoot $stagingRepo)) {
            $runtimeMissing = $true
            $runtimeRoot = $null
        }
    }

    if ($runtimeMissing) {
        $fallbackExe = & py -3.12 -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -ne 0 -or -not $fallbackExe) { throw '缺少可執行的 Windows Python 3.12 pytest runner' }
        $runtimeRoot = Join-Path $scratchRoot 'python-3.12-fallback'
        $null = New-Item -ItemType Directory -Path $runtimeRoot -Force
        & py -3.12 -m venv --system-site-packages $runtimeRoot
        if ($LASTEXITCODE -ne 0) { throw '無法建立暫存 Python fallback venv' }
        $fallbackPython = Join-Path $runtimeRoot 'Scripts\python.exe'
        $sitePath = & $fallbackPython -c "import sysconfig; print(sysconfig.get_path('purelib'))"
        $null = New-Item -ItemType Directory -Path $sitePath -Force
        [System.IO.File]::WriteAllText((Join-Path $sitePath 'serialwrap_repo.pth'), $stagingRepo, [System.Text.UTF8Encoding]::new($false))
        if (-not (Test-Path -LiteralPath $fallbackPython -PathType Leaf)) { throw 'Python fallback venv 缺少 python.exe' }
        $runtimeExe = $fallbackPython
    }

    if (-not $runtimeExe) {
        $runtimeExe = Join-Path $runtimeRoot 'python.exe'
        if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
            $runtimeExe = Join-Path $runtimeRoot 'Scripts\python.exe'
        }
    }
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) { throw "delegate runtime 不存在：$runtimeRoot" }
    $bundleRoot = New-WindowsDevBundle $bundleRoot $rustExecutable $stagingRepo $runtimeRoot $runtimeExe $revision $contractsRevision
    $bundleBin = Join-Path $bundleRoot 'bin'

    $aJunit = Join-Path $outputRoot 'a.xml'
    $l1Directory = Join-Path $outputRoot 'l1\windows\blackbox'
    $null = New-Item -ItemType Directory -Path $l1Directory -Force
    $bJunit = Join-Path $l1Directory 'junit.xml'
    $bCompareJunit = Join-Path $outputRoot 'b.xml'
    $resourceReport = Join-Path $l1Directory 'resource-report.json'
    $pythonCli = Join-Path $stagingRepo 'serialwrap.py'
    $pythonDaemon = Join-Path $stagingRepo 'serialwrapd.py'
    $baselineListeners = Get-LoopbackListenerSnapshot
    $a = Invoke-Blackbox 'a-python-win' $aJunit $pythonCli $pythonDaemon $runtimeExe 'python-win' $false $null $runtimeMissing $bundleRoot
    $releaseCheck = Confirm-BlackboxRelease $stagingRepo $scratchRoot $baselineListeners
    Write-Json (Join-Path $outputRoot 'release-check.json') $releaseCheck
    if ($releaseCheck.ok) {
        $b = Invoke-Blackbox 'b-rust-win' $bJunit (Join-Path $bundleBin 'serialwrap.exe') $pythonDaemon $runtimeExe $Label $true $resourceReport $runtimeMissing $bundleRoot
        if (Test-Path -LiteralPath $bJunit -PathType Leaf) {
            Copy-Item -LiteralPath $bJunit -Destination $bCompareJunit -Force
        }
    } else {
        $missing.Add('A 輪後釋放檢查失敗；依序 A/B 規則略過 B')
        $b = [ordered]@{ exit = 1; junit = $bJunit; stdout = $null; stderr = $null; l1_report = $null; skipped = $true }
    }
    foreach ($requiredFile in @($aJunit, $bJunit, $resourceReport)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            $missing.Add("必要黑箱/L1 證據不存在：$requiredFile")
        }
    }

    $comStatus = 'BLOCKED-OWNER'
    if ($ComPair) {
        if ($ComPair -notmatch '^COM\d+:COM\d+$') {
            $comStatus = 'INVALID_PAIR'
            $missing.Add('SW_TEST_COM_PAIR 格式應為 COMa:COMb')
        } elseif (-not (Get-Command 'uart-stdio-bridge' -ErrorAction SilentlyContinue)) {
            $comStatus = 'EVIDENCE-INSUFFICIENT'
            $missing.Add('P2 uart-stdio-bridge 尚未進入此 worktree')
        } else {
            $comStatus = 'EVIDENCE-INSUFFICIENT'
            $missing.Add('Windows COM attach/cmd/release/attach 驗收尚未執行')
        }
    }
    if ($missing.Count -gt 0) { $missing | Set-Content -LiteralPath (Join-Path $outputRoot 'missing-artifacts.txt') -Encoding UTF8 }
    $report = [ordered]@{
        run_id = $runId
        python = $a
        rust = $b
        a_junit = $aJunit
        b_junit = $bJunit
        b_compare_junit = $bCompareJunit
        l1_directory = $l1Directory
        resource_report = $resourceReport
        release_check = $releaseCheck
        dialect_runtime_missing = [bool]$runtimeMissing
        windows_com = $comStatus
        evidence_insufficient = @($missing)
    }
    Write-Json (Join-Path $outputRoot 'report.json') $report
    Write-Output (Join-Path $outputRoot 'report.json')
    if ($a.exit -ne 0 -or $b.exit -ne 0 -or -not $releaseCheck.ok -or
        ($ComPair -and $comStatus -ne 'PASS') -or $missing.Count -gt 0) { exit 1 }
} finally {
    Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
}
