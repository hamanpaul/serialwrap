param(
    [Parameter(Mandatory = $true)][string]$RustExe,
    [Parameter(Mandatory = $true)][string]$RepoSnapshot,
    [Parameter(Mandatory = $true)][string]$GitBundle,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$Python3123,
    [string]$GitRevision,
    [string]$ContractRevision,
    [string]$ComPair,
    [int]$SelftestTimeoutSeconds = 900,
    [string]$Label = 'rust-1a-win'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($ComPair -and $ComPair -cne 'COM20:COM21') {
    throw '此 Stage 1A runner 僅允許 DUT-side COM pair COM20:COM21'
}

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

function Initialize-StagingGit([string]$GitBundle, [string]$StagingRepo, [string]$Revision, [string]$ScratchRoot) {
    if (-not (Test-Path -LiteralPath $GitBundle -PathType Leaf)) { throw "Git bundle 不存在：$GitBundle" }
    $localBundle = Join-Path $ScratchRoot 'source-revision.bundle'
    Copy-Item -LiteralPath $GitBundle -Destination $localBundle -Force
    & git -C $StagingRepo init --quiet
    if ($LASTEXITCODE -ne 0) { throw '無法初始化 Windows staging repo 的 Git metadata' }
    & git -C $StagingRepo fetch --quiet --no-tags $localBundle HEAD
    if ($LASTEXITCODE -ne 0) { throw "無法從本機 Git bundle 取得 revision：$Revision" }
    $fetchedRevision = (& git -C $StagingRepo rev-parse FETCH_HEAD | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or $fetchedRevision -cne $Revision) {
        throw "Git bundle revision 不符：預期 $Revision，實際 $fetchedRevision"
    }
    & git -C $StagingRepo update-ref refs/heads/sw-staging $fetchedRevision
    if ($LASTEXITCODE -ne 0) { throw "無法設定 staging repo HEAD：$Revision" }
    & git -C $StagingRepo symbolic-ref HEAD refs/heads/sw-staging
    if ($LASTEXITCODE -ne 0) { throw '無法將 staging repo HEAD 指向測試 revision' }
}

function Get-LoopbackListenerSnapshot() {
    if (-not (Get-Command 'Get-NetTCPConnection' -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ ok = $false; listeners = @(); error = 'Get-NetTCPConnection 不可用' }
    }
    try {
        # wslrelay.exe 是 WSL localhost 轉送：它代表 WSL 端（Linux）行程開的 listener，會隨同機其他
        # Linux 工作增減，與本輪 Windows throwaway daemon 無關；不排除會讓釋放檢查誤判。
        $relayPids = @(Get-Process -Name 'wslrelay' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1' } |
            Where-Object { $_.OwningProcess -notin $relayPids } |
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
    # Windows daemon 執行期依賴 PyYAML 與 pyserial（COM 後端）；兩者可能裝在 user site 而非 purelib，
    # 以實際匯入位置補入 ._pth，避免內嵌 runtime 開 COM 時 ATTACH_FAILED:OSError（SC-18）。
    $dependencyDirs = & py -3.12 -c "import os, serial, yaml; print('\n'.join(sorted({os.path.dirname(os.path.dirname(m.__file__)) for m in (serial, yaml)})))"
    if ($LASTEXITCODE -ne 0) { return $false }
    foreach ($dependencyDir in @($dependencyDirs)) {
        $trimmed = ([string]$dependencyDir).Trim()
        if ($trimmed -and -not $lines.Contains($trimmed)) { $lines.Add($trimmed) }
    }
    $lines.Add('import site')
    [System.IO.File]::WriteAllLines($pth.FullName, $lines, [System.Text.UTF8Encoding]::new($false))
    & (Join-Path $RuntimeRoot 'python.exe') -I -c 'import yaml, serial'
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
    $daemonScript = Join-Path $versionRoot 'serialwrapd.py'
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'serialwrapd.py') -Destination $daemonScript -Force
    Copy-RepoTree (Join-Path $RepoRoot 'sw_core') (Join-Path $versionRoot 'sw_core')

    $pythonRelative = if (Test-Path -LiteralPath (Join-Path $pythonRoot 'python.exe')) {
        'py\python.exe'
    } else {
        'py\Scripts\python.exe'
    }
    $runtimeExe = Join-Path $pythonRoot $pythonRelative.Substring(3)
    $runtimeHash = Get-Sha256 $runtimeExe
    $cliHash = Get-Sha256 (Join-Path $versionBinRoot 'serialwrap.exe')
    $launcherPath = 'serialwrapd.py'
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
                launcher = [ordered]@{ path = $launcherPath; sha256 = Get-Sha256 $daemonScript }
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
        # OD-18 (3)：指標 `dir` 一律正斜線（launcher.rs 拒收含 `\` 的 dir）；僅改 JSON 字串，上方 Join-Path 檔案系統路徑不變。
        dir = "versions/$version"
    })
    return $BundleRoot
}

function Invoke-BridgeSelftest([string]$StagingRepo, [string]$ScratchRoot, [string]$OutputRoot,
                               [string]$ComPair, [int]$TimeoutSeconds) {
    # selftest 的 --out、stdout 與 stderr 一律放在 Windows 本機 scratch（$ScratchRoot），不得指向 OutputRoot 或網路路徑。
    # 理由：ST-04 會對 --out 持續寫入 50 MB stderr 證據；網路路徑上的寫入曾發生截斷（dut.stderr.log 僅 48431104 bytes）
    # 與 HARNESS_EVIDENCE_WRITE_FAILED（bridge rc=3）。行程結束後才整份回寫 OutputRoot\windows-com\bridge-selftest，
    # 並逐檔比對 SHA256。資格判定（is_qualified、verdict、tree_sha256）仍只讀本機報告，回寫結果不參與放行。
    $evidenceRoot = Join-Path $OutputRoot 'windows-com'
    $selftestRoot = Join-Path $ScratchRoot 'bridge-selftest'
    $reportPath = Join-Path $selftestRoot 'selftest-report.json'
    $copyRoot = Join-Path $evidenceRoot 'bridge-selftest'
    $copyReportPath = Join-Path $copyRoot 'selftest-report.json'
    $null = New-Item -ItemType Directory -Path $evidenceRoot -Force
    $null = New-Item -ItemType Directory -Path $selftestRoot -Force
    $bridgeSource = Join-Path $StagingRepo 'tools/uart_stdio_bridge/src'
    $verifyScript = Join-Path $ScratchRoot 'verify-bridge-selftest.py'
    $verifyCode = @'
import json
import platform
from pathlib import Path
import sys
from uart_stdio_bridge.identity import tool_tree_sha256
from uart_stdio_bridge.selftest.runner import is_qualified, load_approved_revision
report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
approved = load_approved_revision()
current_tree = tool_tree_sha256()
qualified = is_qualified(
    report,
    endpoint="win-serial",
    tool_tree_sha256=current_tree,
    platform_os=platform.system(),
    approved=approved,
)
# 只輸出診斷，不參與判定：讓 selftest-result.json 能區分「raw selftest 失敗」與「核准 tree 不符」。
report_tree = (report.get("tool") or {}).get("tree_sha256") if isinstance(report.get("tool"), dict) else None
print(json.dumps({
    "qualified": qualified,
    "raw_verdict": report.get("verdict"),
    "raw_complete": report.get("complete"),
    "report_tree_sha256": report_tree,
    "current_tree_sha256": current_tree,
    "approved_tree_sha256": approved.get("tree_sha256"),
    "tree_approved": report_tree == approved.get("tree_sha256") == current_tree,
    "test_overrides": report.get("test_overrides"),
}, ensure_ascii=False, sort_keys=True))
raise SystemExit(0 if qualified else 1)
'@
    [System.IO.File]::WriteAllText($verifyScript, $verifyCode, [System.Text.UTF8Encoding]::new($false))
    $oldPythonPath = $env:PYTHONPATH
    $bridgeOverrides = @(Get-ChildItem Env: | Where-Object { $_.Name -like 'UART_STDIO_BRIDGE_TEST_*' })
    foreach ($item in $bridgeOverrides) { Remove-Item -LiteralPath "Env:$($item.Name)" }
    $env:PYTHONPATH = $bridgeSource
    $oldErrorActionPreference = $ErrorActionPreference
    $selftestExit = 1
    $qualifiedExit = 1
    $qualificationOutput = @()
    $selftestLog = Join-Path $ScratchRoot 'bridge-selftest.stdout.log'
    $selftestError = Join-Path $ScratchRoot 'bridge-selftest.stderr.log'
    if ($TimeoutSeconds -lt 1) { throw 'SelftestTimeoutSeconds 必須是正整數秒數' }
    $timeoutSeconds = $TimeoutSeconds
    $timedOut = $false
    $selftestProcessId = $null
    # ExitCode 取不到（null）時必須判定為失敗；不得以 [int]$null（= 0）放行。
    $exitStatusKnown = $true
    try {
        $pyCommand = Get-Command 'py.exe' -ErrorAction Stop
        $arguments = '-3.12 -m uart_stdio_bridge selftest --endpoint win-serial --com-pair {0} --transport-mode virtual_com0com --out "{1}"' -f $ComPair, $selftestRoot
        $process = Start-Process -FilePath $pyCommand.Source -ArgumentList $arguments `
            -WorkingDirectory $StagingRepo -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $selftestLog -RedirectStandardError $selftestError
        # Windows PowerShell 5.1 必須在行程結束前快取 Handle，否則 ExitCode 會讀成 null。
        $null = $process.Handle
        $selftestProcessId = [int]$process.Id
        if (-not $process.WaitForExit($timeoutSeconds * 1000)) {
            $timedOut = $true
            & taskkill.exe /PID $process.Id /T /F *> $null
            $null = $process.WaitForExit(10000)
            $selftestExit = 124
        } else {
            $rawSelftestExit = $process.ExitCode
            if ($null -eq $rawSelftestExit) {
                $exitStatusKnown = $false
                $selftestExit = 1
            } else {
                $selftestExit = [int]$rawSelftestExit
            }
        }
        if (-not $timedOut -and (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            $qualificationOutput = & py -3.12 $verifyScript $reportPath 2>&1
            $qualifiedExit = [int]$LASTEXITCODE
        }
    } finally {
        if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $oldPythonPath }
        foreach ($item in $bridgeOverrides) { Set-Item -LiteralPath "Env:$($item.Name)" -Value $item.Value }
        $ErrorActionPreference = $oldErrorActionPreference
    }
    $report = $null
    if (-not $timedOut -and (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
        $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    $ok = ($exitStatusKnown -and $selftestExit -eq 0 -and $qualifiedExit -eq 0 -and $report -and $report.verdict -eq 'PASS')
    # 行程結束後回寫（逾時或失敗路徑亦然，保留部分工作證據）。回寫只產生證據副本，不改寫報告內容，也不參與上面的判定。
    # F8：有界重試（最多 3 次；每次整份重新複製再逐檔比對），任一次全部相符即止；仍不符只會使證據不足，不會放行。
    # 比對範圍：selftest 工作目錄與報告，以及 stdout／stderr 兩份 log（副本位於 evidenceRoot 同名檔）。
    $copyErrors = [System.Collections.Generic.List[string]]::new()
    for ($copyAttempt = 1; $copyAttempt -le 3; $copyAttempt++) {
        $copyErrors = [System.Collections.Generic.List[string]]::new()
        try {
            $null = New-Item -ItemType Directory -Path $copyRoot -Force
            $copyWork = Join-Path $copyRoot 'work'
            if (Test-Path -LiteralPath $copyWork) { Remove-Item -LiteralPath $copyWork -Recurse -Force }
            if (Test-Path -LiteralPath (Join-Path $selftestRoot 'work') -PathType Container) {
                Copy-Item -LiteralPath (Join-Path $selftestRoot 'work') -Destination $copyWork -Recurse -Force
            }
            if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
                Copy-Item -LiteralPath $reportPath -Destination $copyReportPath -Force
            }
            foreach ($log in @($selftestLog, $selftestError)) {
                if (Test-Path -LiteralPath $log -PathType Leaf) {
                    $logCopy = Join-Path $evidenceRoot (Split-Path -Leaf $log)
                    Copy-Item -LiteralPath $log -Destination $logCopy -Force
                    if (-not (Test-Path -LiteralPath $logCopy -PathType Leaf)) {
                        $copyErrors.Add("回寫缺檔：$(Split-Path -Leaf $log)")
                    } elseif ((Get-Sha256 $log) -ne (Get-Sha256 $logCopy)) {
                        $copyErrors.Add("回寫 SHA256 不符：$(Split-Path -Leaf $log)")
                    }
                }
            }
            # 逐檔比對：本機來源與回寫副本的 SHA256 必須一致；不一致即視為回寫失敗，只會使證據不足，不會放行。
            if (Test-Path -LiteralPath $selftestRoot -PathType Container) {
                foreach ($file in @(Get-ChildItem -LiteralPath $selftestRoot -Recurse -File -Force)) {
                    $relative = $file.FullName.Substring($selftestRoot.Length).TrimStart('\', '/')
                    $copied = Join-Path $copyRoot $relative
                    if (-not (Test-Path -LiteralPath $copied -PathType Leaf)) {
                        $copyErrors.Add("回寫缺檔：$relative")
                    } elseif ((Get-Sha256 $file.FullName) -ne (Get-Sha256 $copied)) {
                        $copyErrors.Add("回寫 SHA256 不符：$relative")
                    }
                }
            }
        } catch {
            $copyErrors.Add("回寫例外：$($_.Exception.Message)")
        }
        if ($copyErrors.Count -eq 0) { break }
        if ($copyAttempt -lt 3) { Start-Sleep -Milliseconds 500 }
    }
    $copyOk = ($copyErrors.Count -eq 0)
    # raw selftest 成功即可執行診斷；qualification（含核准 tree）仍由 status 單獨決定。
    $diagnosticReady = ($exitStatusKnown -and $selftestExit -eq 0 -and $report -and $report.verdict -eq 'PASS' -and $report.complete -eq $true)
    $status = if (-not $ok) { if ($report) { 'FAIL' } else { 'EVIDENCE-INSUFFICIENT' } } elseif ($copyOk) { 'PASS' } else { 'EVIDENCE-INSUFFICIENT' }
    $reason = if ($timedOut) { 'S0 selftest 逾時，沒有完成報告；保留部分工作證據並停止子行程樹' } elseif (-not $exitStatusKnown) { 'S0 selftest 行程 ExitCode 不可得（null），視為失敗' } elseif (-not $report) { 'S0 selftest 未產生完成報告' } elseif ($selftestExit -ne 0 -or $report.verdict -ne 'PASS') { 'S0 selftest raw 結果失敗（exit=' + $selftestExit + '、verdict=' + $report.verdict + '）' } elseif ($qualifiedExit -ne 0) { 'S0 selftest 資格檢查未通過（qualification_exit=' + $qualifiedExit + '）；raw verdict 與核准 tree 比對見 qualification_output' } elseif (-not $copyOk) { 'S0 selftest 報告已產生，但回寫 OutDir 證據不完整：' + ($copyErrors -join '；') } else { $null }
    return [ordered]@{
        status = $status
        diagnostic_ready = $diagnosticReady
        exit = $selftestExit
        exit_status_known = $exitStatusKnown
        qualification_exit = $qualifiedExit
        timeout_seconds = $timeoutSeconds
        timed_out = $timedOut
        process_id = $selftestProcessId
        reason = $reason
        qualification_output = @($qualificationOutput)
        report = $copyReportPath
        local_out = $selftestRoot
        local_report = $reportPath
        evidence_copy = $copyRoot
        evidence_copy_ok = $copyOk
        evidence_copy_errors = @($copyErrors)
        evidence_dir_note = '報告 checks[].evidence_dir 為執行當下的 Windows 本機路徑（原樣保留、未改寫）；可取用副本位於 evidence_copy 的 work 子目錄'
        endpoint = 'win-serial'
        com_pair = $ComPair
        transport_mode = 'virtual_com0com'
        stdout = Join-Path $evidenceRoot 'bridge-selftest.stdout.log'
        stderr = Join-Path $evidenceRoot 'bridge-selftest.stderr.log'
        local_stdout = $selftestLog
        local_stderr = $selftestError
    }
}

function Invoke-Blackbox([string]$Name, [string]$JunitPath, [string]$CliPath, [string]$DaemonPath,
                         [string]$RuntimePath, [string]$RunLabel, [bool]$RustRun,
                         [string]$L1Report, [string]$BundlePath,
                         [string]$ComPair, [string]$DutWire, [string]$PidLog) {
    $env:SERIALWRAP_CLI_EXE = $CliPath
    $env:SERIALWRAP_DAEMON_EXE = $DaemonPath
    $env:SERIALWRAP_AB_LABEL = $RunLabel
    if ($RuntimePath) { $env:SW_BASELINE_PYTHON = $RuntimePath } else { Remove-Item Env:SW_BASELINE_PYTHON -ErrorAction SilentlyContinue }
    if ($BundlePath) { $env:SW_WIN_DEV_BUNDLE = $BundlePath }
    if ($PidLog) { $env:SW_AB_PIDLOG = $PidLog } else { Remove-Item Env:SW_AB_PIDLOG -ErrorAction SilentlyContinue }
    if ($ComPair -and $DutWire) {
        $env:SW_TEST_COM_PAIR = $ComPair
        $env:SW_TEST_DUT_WIRE = $DutWire
    } else {
        Remove-Item Env:SW_TEST_COM_PAIR -ErrorAction SilentlyContinue
        Remove-Item Env:SW_TEST_DUT_WIRE -ErrorAction SilentlyContinue
    }
    if ($L1Report -and (Test-Path -LiteralPath (Join-Path $script:stagingRepo 'tests/resource_report_plugin.py'))) {
        $env:SW_RESOURCE_REPORT = $L1Report
    } else {
        Remove-Item Env:SW_RESOURCE_REPORT -ErrorAction SilentlyContinue
    }
    # pytest argv 與凍結的 s1a_blackbox windows selector 完全一致。
    $arguments = @('-3.12', '-m', 'pytest', 'tests/contract_blackbox', '-m', 'core_portable or windows_native',
                   "--junitxml=$JunitPath")
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

function Start-WindowsComBridge([string]$StagingRepo, [string]$ScratchRoot, [string]$OutputRoot,
                                [string]$ComPair, [string]$RunId) {
    if ($ComPair -cne 'COM20:COM21') { throw '拒絕開啟非 COM20:COM21 配對' }
    $comRoot = Join-Path $OutputRoot 'windows-com'
    $bridgeRoot = Join-Path $comRoot 'bridge'
    $null = New-Item -ItemType Directory -Path $bridgeRoot -Force
    # bridge 證據（wire／control.json 皆 fsync）寫在 Windows 本機 scratch，停止後再回寫 OutputRoot：
    # \\wsl.localhost（9P）上 FlushFileBuffers 失敗時 CRT 一律回 EBADF，曾使 bridge 未建 control.json 即以 exit 3 結束。
    $localBridgeRoot = Join-Path $ScratchRoot 'windows-com-bridge'
    $null = New-Item -ItemType Directory -Path $localBridgeRoot -Force
    $bridgeSource = Join-Path $StagingRepo 'tools/uart_stdio_bridge/src'
    $runScript = Join-Path $ScratchRoot 'run-uart-stdio-bridge.py'
    $specPath = Join-Path $ScratchRoot 'run-uart-stdio-bridge.json'
    $wirePath = Join-Path $localBridgeRoot 'wire.ndjson'
    $controlPath = Join-Path $localBridgeRoot 'control.json'
    $stdoutPath = Join-Path $localBridgeRoot 'stdout.log'
    $stderrPath = Join-Path $localBridgeRoot 'stderr.log'
    $dutArgv = ConvertTo-Json -InputObject @('py', '-3.12', '-m', 'uart_stdio_bridge.dut', '--mode', 'echo') -Compress
    $bridgeArgs = @(
        'run', '--run-id', "g1-$RunId", '--case-id', 'stage1a-windows-com', '--port-id', 'COM0',
        '--evidence-dir', $localBridgeRoot, '--endpoint', 'win-serial', '--com', 'COM21',
        '--transport-mode', 'virtual_com0com', '--dut-mode', 'local-process', '--dut-argv-json', $dutArgv
    )
    Write-Json $specPath ([ordered]@{ argv = $bridgeArgs })
    $runnerCode = @'
import json
import sys
from pathlib import Path
from uart_stdio_bridge.cli import main
arguments = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["argv"]
raise SystemExit(main(arguments))
'@
    [System.IO.File]::WriteAllText($runScript, $runnerCode, [System.Text.UTF8Encoding]::new($false))
    $pythonExeLines = & py -3.12 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not $pythonExeLines) { throw '找不到原生 Windows Python 3.12，無法啟動 COM bridge' }
    $pythonExe = [string]($pythonExeLines | Select-Object -Last 1)
    $pythonExe = $pythonExe.Trim()
    $oldPythonPath = $env:PYTHONPATH
    $bridgeOverrides = @(Get-ChildItem Env: | Where-Object { $_.Name -like 'UART_STDIO_BRIDGE_TEST_*' })
    foreach ($item in $bridgeOverrides) { Remove-Item -LiteralPath "Env:$($item.Name)" }
    $env:PYTHONPATH = $bridgeSource
    try {
        $runnerArgs = '"{0}" "{1}"' -f $runScript, $specPath
        $process = Start-Process -FilePath $pythonExe -ArgumentList $runnerArgs -WorkingDirectory $StagingRepo `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        # 快取 Handle 後 Stop 階段才讀得到 ExitCode（同 Invoke-BridgeSelftest）。
        $null = $process.Handle
    } finally {
        if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $oldPythonPath }
        foreach ($item in $bridgeOverrides) { Set-Item -LiteralPath "Env:$($item.Name)" -Value $item.Value }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $controlPath -PathType Leaf) {
            return [ordered]@{
                process = $process
                python = $pythonExe
                source = $bridgeSource
                evidence_dir = $localBridgeRoot
                output_dir = $bridgeRoot
                wire = $wirePath
                control = $controlPath
                stdout = $stdoutPath
                stderr = $stderrPath
                transport_mode = 'virtual_com0com'
            }
        }
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    $reason = 'uart_stdio_bridge run 未在 30 秒內建立控制端點'
    if ($process.HasExited) { $reason += "；exit=$($process.ExitCode)" }
    if (-not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F *> $null
        $null = $process.WaitForExit(10000)
    }
    $copyErrors = @(Copy-WindowsComBridgeEvidence $localBridgeRoot $bridgeRoot)
    if ($copyErrors.Count -gt 0) { $reason += "；證據回寫不完整：$($copyErrors -join '；')" }
    throw $reason
}

function Copy-WindowsComBridgeEvidence([string]$LocalRoot, [string]$OutRoot) {
    # 整份回寫並逐檔比對 SHA256；有界重試 3 次，任一次全部相符即止。回傳錯誤清單（空＝成功）。
    $copyErrors = [System.Collections.Generic.List[string]]::new()
    for ($copyAttempt = 1; $copyAttempt -le 3; $copyAttempt++) {
        $copyErrors = [System.Collections.Generic.List[string]]::new()
        try {
            $null = New-Item -ItemType Directory -Path $OutRoot -Force
            if (Test-Path -LiteralPath $LocalRoot -PathType Container) {
                foreach ($file in @(Get-ChildItem -LiteralPath $LocalRoot -Recurse -File -Force)) {
                    $relative = $file.FullName.Substring($LocalRoot.Length).TrimStart('\', '/')
                    $copied = Join-Path $OutRoot $relative
                    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $copied) -Force
                    Copy-Item -LiteralPath $file.FullName -Destination $copied -Force
                    if (-not (Test-Path -LiteralPath $copied -PathType Leaf)) {
                        $copyErrors.Add("回寫缺檔：$relative")
                    } elseif ((Get-Sha256 $file.FullName) -ne (Get-Sha256 $copied)) {
                        $copyErrors.Add("回寫 SHA256 不符：$relative")
                    }
                }
            } else {
                $copyErrors.Add("本機 bridge 證據目錄不存在：$LocalRoot")
            }
        } catch {
            $copyErrors.Add("回寫例外：$($_.Exception.Message)")
        }
        if ($copyErrors.Count -eq 0) { break }
        if ($copyAttempt -lt 3) { Start-Sleep -Milliseconds 500 }
    }
    return @($copyErrors)
}

function Invoke-WindowsComSequence([string]$StagingRepo, [string]$ScratchRoot, [string]$OutputRoot,
                                   [string]$RustExe, [string]$PythonExe, [string]$DaemonScript,
                                   [string]$ComPair, [string]$BridgeWire) {
    if ($ComPair -cne 'COM20:COM21') { throw '拒絕對非 COM20:COM21 執行 UART 命令' }
    $comRoot = Join-Path $OutputRoot 'windows-com'
    $sequencePath = Join-Path $comRoot 'sequence.json'
    $specPath = Join-Path $ScratchRoot 'windows-com-sequence.json'
    $scriptPath = Join-Path $ScratchRoot 'windows-com-sequence.py'
    $workRoot = Join-Path $ScratchRoot 'windows-com-daemon'
    $daemonStdout = Join-Path $comRoot 'daemon.stdout.log'
    $daemonStderr = Join-Path $comRoot 'daemon.stderr.log'
    $spec = [ordered]@{
        com_pair = $ComPair
        rust_exe = $RustExe
        python_exe = $PythonExe
        daemon_script = $DaemonScript
        repo = $StagingRepo
        work = $workRoot
        output = $sequencePath
        daemon_stdout = $daemonStdout
        daemon_stderr = $daemonStderr
        bridge_wire = $BridgeWire
    }
    Write-Json $specPath $spec
    $scriptCode = @'
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
work = Path(spec["work"])
work.mkdir(parents=True, exist_ok=True)
for name in ("state", "run", "config", "wal", "logs", "by-id", "by-path", "events", "events-runtime", "home", "xdg-config", "xdg-state"):
    (work / name).mkdir(parents=True, exist_ok=True)
profiles = work / "profiles"
profiles.mkdir(parents=True, exist_ok=True)
(profiles / "live.yaml").write_text(
    "profiles:\n  live-passthrough:\n    platform: passthrough\ntargets:\n  - com: COM0\n    profile: live-passthrough\n    device_by_id: COM20\n",
    encoding="utf-8",
)
config = work / "config" / "config.yaml"
config.write_text("windows:\n  test_device_allowlist:\n    - COM20\n", encoding="utf-8")
env = dict(os.environ)
for key in list(env):
    if key.startswith("SERIALWRAP_"):
        env.pop(key, None)
    if key.startswith("PYTHON") or key.startswith("UART_STDIO_BRIDGE_TEST_"):
        env.pop(key, None)
env.update({
    "SERIALWRAP_STATE_DIR": str(work / "state"),
    "SERIALWRAP_RUN_DIR": str(work / "run"),
    "SERIALWRAP_WAL_DIR": str(work / "wal"),
    "SERIALWRAP_LOG_DIR": str(work / "logs"),
    "SERIALWRAP_CONFIG_DIR": str(work / "config"),
    "SERIALWRAP_CONFIG": str(config),
    "SERIALWRAP_BY_ID_DIR": str(work / "by-id"),
    "SERIALWRAP_BY_PATH_DIR": str(work / "by-path"),
    "SERIALWRAP_EVENTS_DIR": str(work / "events"),
    "SERIALWRAP_EVENTS_RUNTIME_DIR": str(work / "events-runtime"),
    "SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM20",
    "HOME": str(work / "home"),
    "USERPROFILE": str(work / "home"),
    "XDG_CONFIG_HOME": str(work / "xdg-config"),
    "XDG_STATE_HOME": str(work / "xdg-state"),
})
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    endpoint = f"tcp://127.0.0.1:{listener.getsockname()[1]}"

steps = []
daemon = None
passed = False
failure = None

def run_step(name, args, timeout=60):
    argv = [spec["rust_exe"], "--endpoint", endpoint, *args]
    result = subprocess.run(argv, cwd=spec["repo"], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    row = {"name": name, "argv": argv, "returncode": result.returncode,
           "stdout": result.stdout, "stderr": result.stderr}
    steps.append(row)
    if result.returncode != 0:
        raise RuntimeError(f"{name} exit={result.returncode}: {result.stderr[-2000:]}")
    return result

def state_of(output):
    data = json.loads(output)
    return next((row.get("state") for row in data.get("sessions", []) if row.get("com") == "COM0"), None)

def wait_state(expected, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = subprocess.run([spec["rust_exe"], "--endpoint", endpoint, "session", "list"],
                                cwd=spec["repo"], env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15)
        last = {"name": f"session-list-{expected.lower()}",
                "argv": [spec["rust_exe"], "--endpoint", endpoint, "session", "list"],
                "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        if result.returncode == 0:
            steps.append(last)
            if state_of(result.stdout) == expected:
                return
        time.sleep(0.25)
    if last is not None:
        steps.append(last)
    raise RuntimeError(f"COM20 對應 COM0 未進入 {expected}")

try:
    with open(spec["daemon_stdout"], "ab") as stdout, open(spec["daemon_stderr"], "ab") as stderr:
        daemon_argv = [spec["python_exe"], "-B", spec["daemon_script"],
                       "--profile-dir", str(profiles), "--socket", endpoint,
                       "--lock", str(work / "daemon.lock")]
        daemon = subprocess.Popen(daemon_argv, cwd=spec["repo"], env=env, stdout=stdout, stderr=stderr)
    steps.append({"name": "python-daemon-start", "argv": daemon_argv, "pid": daemon.pid})
    wait_state("READY")

    command = run_step("cmd-submit", ["cmd", "submit", "--selector", "COM0", "--cmd", "echo hi",
                                      "--completion-regex", "echo hi", "--cmd-timeout", "15"], timeout=60)
    command_id = json.loads(command.stdout).get("cmd_id")
    if not command_id:
        raise RuntimeError("cmd submit 回應缺少 cmd_id")
    status_deadline = time.monotonic() + 20
    command_data = None
    while time.monotonic() < status_deadline:
        status = run_step("cmd-status-poll", ["cmd", "status", "--cmd-id", command_id], timeout=30)
        status_data = json.loads(status.stdout)
        command_data = status_data.get("command", status_data)
        command_status = command_data.get("status")
        if command_status == "done":
            break
        if command_status in {"failed", "cancelled", "timed_out"}:
            raise RuntimeError(f"cmd status 終止於 {command_status}")
        time.sleep(0.25)
    if not command_data or command_data.get("status") != "done":
        observed = command_data.get("status") if command_data else "missing"
        raise RuntimeError(f"cmd status 未在期限內回報 done：{observed}")

    run_step("device-release", ["device", "release", "--selector", "COM0"])
    wait_state("RELEASED", timeout=30)
    run_step("device-attach", ["device", "attach", "--selector", "COM0"])
    wait_state("READY")
    passed = True
except Exception as exc:
    failure = f"{type(exc).__name__}: {exc}"
finally:
    if daemon is not None and daemon.poll() is None:
        try:
            argv = [spec["rust_exe"], "--endpoint", endpoint, "daemon", "stop"]
            result = subprocess.run(argv, cwd=spec["repo"], env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
            steps.append({"name": "daemon-stop", "argv": argv, "returncode": result.returncode,
                          "stdout": result.stdout, "stderr": result.stderr})
            daemon.wait(timeout=20)
        except Exception as exc:
            failure = failure or f"daemon cleanup {type(exc).__name__}: {exc}"
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=10)

report = {
    "format": "windows-com-sequence.v1",
    "result": "PASS" if passed and failure is None else "FAIL",
    "com_pair": spec["com_pair"],
    "serialwrap_com": "COM20",
    "bridge_com": "COM21",
    "transport_mode": "virtual_com0com",
    "bridge_wire": spec["bridge_wire"],
    "daemon_pid": daemon.pid if daemon is not None else None,
    "daemon_exit_code": daemon.returncode if daemon is not None else None,
    "daemon_stdout": spec["daemon_stdout"],
    "daemon_stderr": spec["daemon_stderr"],
    "steps": steps,
    "failure": failure,
}
Path(spec["output"]).write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
raise SystemExit(0 if report["result"] == "PASS" else 1)
'@
    [System.IO.File]::WriteAllText($scriptPath, $scriptCode, [System.Text.UTF8Encoding]::new($false))
    $pythonOutput = & py -3.12 $scriptPath $specPath 2>&1
    $sequenceExit = [int]$LASTEXITCODE
    $sequence = $null
    if (Test-Path -LiteralPath $sequencePath -PathType Leaf) {
        $sequence = Get-Content -LiteralPath $sequencePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    return [ordered]@{
        status = if ($sequence -and $sequence.result -eq 'PASS' -and $sequenceExit -eq 0) { 'PASS' } elseif ($sequence) { 'FAIL' } else { 'EVIDENCE-INSUFFICIENT' }
        exit = $sequenceExit
        path = $sequencePath
        report = $sequence
        output = @($pythonOutput)
    }
}

function Stop-WindowsComBridge([object]$Bridge, [string]$ScratchRoot) {
    $controlScript = Join-Path $ScratchRoot 'stop-uart-stdio-bridge.py'
    $controlCode = @'
import json
import sys
from pathlib import Path
from uart_stdio_bridge.bridge.control import ControlClient
client = ControlClient(Path(sys.argv[1]), timeout_s=30.0)
try:
    print(json.dumps(client.call("shutdown"), sort_keys=True))
finally:
    client.close()
'@
    [System.IO.File]::WriteAllText($controlScript, $controlCode, [System.Text.UTF8Encoding]::new($false))
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $Bridge.source
    $controlExit = 1
    try {
        $controlOutput = & $Bridge.python $controlScript $Bridge.control 2>&1
        $controlExit = [int]$LASTEXITCODE
    } finally {
        if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $oldPythonPath }
    }
    $exited = $Bridge.process.WaitForExit(30000)
    if (-not $exited) {
        & taskkill.exe /PID $Bridge.process.Id /T /F *> $null
        $exited = $Bridge.process.WaitForExit(10000)
    }
    if (Test-Path -LiteralPath $Bridge.control -PathType Leaf) {
        Remove-Item -LiteralPath $Bridge.control -Force
    }
    # 行程結束後才把本機證據回寫 OutputRoot；回寫不完整只會使證據不足，不會放行。
    $copyErrors = @(Copy-WindowsComBridgeEvidence $Bridge.evidence_dir $Bridge.output_dir)
    $copyOk = ($copyErrors.Count -eq 0)
    # ExitCode 為 null 時必須判定為失敗（fail closed），不得與 0 比較後被當成通過。
    $processExit = if ($exited) { $Bridge.process.ExitCode } else { $null }
    $processExitKnown = ($null -ne $processExit)
    $status = if ($controlExit -eq 0 -and $exited -and $processExitKnown -and $processExit -eq 0) { 'PASS' } else { 'FAIL' }
    if ($status -eq 'PASS' -and -not $copyOk) { $status = 'EVIDENCE-INSUFFICIENT' }
    return [ordered]@{
        status = $status
        evidence_copy_ok = $copyOk
        evidence_copy_errors = @($copyErrors)
        evidence_dir = $Bridge.evidence_dir
        output_dir = $Bridge.output_dir
        control_exit = $controlExit
        process_exit = $processExit
        process_exit_known = $processExitKnown
        output = @($controlOutput)
        stdout = (Join-Path $Bridge.output_dir 'stdout.log')
        stderr = (Join-Path $Bridge.output_dir 'stderr.log')
    }
}

$sourceRoot = (Resolve-Path -LiteralPath $RepoSnapshot).ProviderPath
$rustExecutable = (Resolve-Path -LiteralPath $RustExe).ProviderPath
if (-not (Test-Path -LiteralPath $rustExecutable -PathType Leaf)) { throw "Rust CLI 不存在：$RustExe" }
function Copy-BlackboxDiagnostics([string]$StagingRepo, [string]$BundleRoot, [string]$OutputRoot) {
    # ab-suite 輸出（A/B 執行器）位於 staging 的 l1\windows\blackbox\ab-suite\（與 SW_RESOURCE_REPORT 同層）；
    # Windows dev bundle 亦只存在於 scratch。staging 於 finally 刪除前必須複製回 OutDir，否則失敗時無法診斷，
    # register 也拿不到 --windows-cli（plan 04 Task 17）。
    $abSource = Join-Path $StagingRepo 'l1\windows\blackbox\ab-suite'
    if (Test-Path -LiteralPath $abSource -PathType Container) {
        $abDestination = Join-Path $OutputRoot 'l1\windows\blackbox\ab-suite'
        if (Test-Path -LiteralPath $abDestination) { Remove-Item -LiteralPath $abDestination -Recurse -Force }
        $null = New-Item -ItemType Directory -Path (Split-Path -Parent $abDestination) -Force
        Copy-Item -LiteralPath $abSource -Destination $abDestination -Recurse -Force
    }
    # 匯出 Windows dev bundle 到 OutDir\bundle\。versions\<V>\bin\serialwrap.exe 為 release-manifest 的 cli.path 所指，
    # register 的 Rust CLI artifact 必須是它（與 manifest 路徑相符），bin\serialwrap.exe 為同一位元內容的入口複本。
    if (Test-Path -LiteralPath (Join-Path $BundleRoot 'bin\serialwrap.exe') -PathType Leaf) {
        $bundleVersion = (Get-Content -LiteralPath (Join-Path $StagingRepo 'VERSION') -Raw).Trim()
        $bundleFiles = @('bin\serialwrap.exe', 'bin\current-release.json',
                         "versions\$bundleVersion\release-manifest.json",
                         "versions\$bundleVersion\bin\serialwrap.exe")
        $bundleDestination = Join-Path $OutputRoot 'bundle'
        foreach ($relative in $bundleFiles) {
            # 組裝中途失敗時（例如 current-release.json 尚未寫出）逐檔檢查來源，缺者只警告略過。
            $source = Join-Path $BundleRoot $relative
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                Write-Warning "bundle 檔案不存在，略過匯出：$relative"
                continue
            }
            $destination = Join-Path $bundleDestination $relative
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force
            Copy-Item -LiteralPath $source -Destination $destination -Force
        }
    }
}

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
$bridge = $null
$bridgeStop = $null
$selftest = $null
$comSequence = $null
$comStatus = 'BLOCKED-OWNER'

try {
    # scripts/rust 三檔為 tests/contract_blackbox 的 A/B 與 P4 執行器所需（惰性載入）；缺漏即視為 snapshot 不完整。
    $requiredPaths = @('serialwrap.py', 'serialwrapd.py', 'sw_core', 'tests/contract_blackbox',
                       'tests/ab_env.py', 'VERSION', 'contracts',
                       'scripts/rust/ab_blackbox_1b.py', 'scripts/rust/ab_compare.py', 'scripts/rust/check_p4_cases.py',
                       'scripts/rust/owner_gated.py')
    foreach ($relative in $requiredPaths) { $null = Copy-RepoPath $sourceRoot $relative $stagingRepo $true }
    if ($ComPair) { $null = Copy-RepoPath $sourceRoot 'tools/uart_stdio_bridge' $stagingRepo $true }
    # P4-T16：test_cli_mgmt_ab_suite 由凍結鎖讀 AB_CASES 並收集每個凍結 pytest 節點；
    # 鎖檔與節點所在檔（含 tests/contract_blackbox 以外者）一併放入 snapshot，缺少即視為 snapshot 不完整。
    $null = Copy-RepoPath $sourceRoot 'tests/acceptance' $stagingRepo $true
    $p4Lock = Join-Path $sourceRoot 'tests/acceptance/p4_required.lock.json'
    $p4Files = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($case in (Get-Content -LiteralPath $p4Lock -Raw -Encoding UTF8 | ConvertFrom-Json).cases) {
        foreach ($node in @($case.tests)) {
            if ($node -is [string] -and $node.StartsWith('tests/') -and $node.Contains('::')) {
                $file = $node.Substring(0, $node.IndexOf('::'))
                if (-not $file.StartsWith('tests/contract_blackbox/')) { $null = $p4Files.Add($file) }
            }
        }
    }
    foreach ($relative in $p4Files) { $null = Copy-RepoPath $sourceRoot $relative $stagingRepo $true }
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
    Initialize-StagingGit $GitBundle $stagingRepo $revision $scratchRoot

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
    # 供 WSL 端 win-blackbox.sh 產生 A/B identity（daemon tree／runtime sha256 取自此 manifest）。
    $bundleVersion = (Get-Content -LiteralPath (Join-Path $stagingRepo 'VERSION') -Raw).Trim()
    $bundleDaemon = Join-Path $bundleRoot "versions\$bundleVersion\serialwrapd.py"
    Copy-Item -LiteralPath (Join-Path $bundleRoot "versions\$bundleVersion\release-manifest.json") `
        -Destination (Join-Path $outputRoot 'windows-release-manifest.json') -Force

    $aJunit = Join-Path $outputRoot 'a.xml'
    $l1Directory = Join-Path $outputRoot 'l1\windows\blackbox'
    $null = New-Item -ItemType Directory -Path $l1Directory -Force
    # B 輪的 junit 與 SW_RESOURCE_REPORT 必須位於 staging（tests/contract_blackbox/test_cli_mgmt_ab_suite.py
    # 要求 A/B 證據路徑位於本 worktree）；B 輪結束後再複製到 OutDir 的 l1\windows\blackbox\（plan 04 Task 17）。
    $stagingL1Directory = Join-Path $stagingRepo 'l1\windows\blackbox'
    $null = New-Item -ItemType Directory -Path $stagingL1Directory -Force
    $bStagingJunit = Join-Path $stagingL1Directory 'junit.xml'
    $stagingResourceReport = Join-Path $stagingL1Directory 'resource-report.json'
    $bJunit = Join-Path $l1Directory 'junit.xml'
    $bCompareJunit = Join-Path $outputRoot 'b.xml'
    $resourceReport = Join-Path $l1Directory 'resource-report.json'
    $pythonCli = Join-Path $stagingRepo 'serialwrap.py'
    $pythonDaemon = Join-Path $stagingRepo 'serialwrapd.py'
    if ($ComPair) {
        $selftest = Invoke-BridgeSelftest $stagingRepo $scratchRoot $outputRoot $ComPair $SelftestTimeoutSeconds
        Write-Json (Join-Path $outputRoot 'windows-com\selftest-result.json') $selftest
        if ($selftest.status -eq 'FAIL') {
            $missing.Add('Windows bridge selftest 實際失敗或資格檢查未通過')
        } elseif ($selftest.status -ne 'PASS') {
            $missing.Add('Windows bridge selftest 未產生完整合格證據')
        }
    }
    $baselineListeners = Get-LoopbackListenerSnapshot
    $a = Invoke-Blackbox 'a-python-win' $aJunit $pythonCli $pythonDaemon $runtimeExe 'python-win' $false `
        $null $bundleRoot $null $null $null
    $releaseCheck = Confirm-BlackboxRelease $stagingRepo $scratchRoot $baselineListeners
    Write-Json (Join-Path $outputRoot 'release-check.json') $releaseCheck
    if ($releaseCheck.ok) {
        if ($ComPair -and $selftest -and $selftest.diagnostic_ready) {
            try {
                $bridge = Start-WindowsComBridge $stagingRepo $scratchRoot $outputRoot $ComPair $runId
                $comSequence = Invoke-WindowsComSequence $stagingRepo $scratchRoot $outputRoot `
                    (Join-Path $bundleBin 'serialwrap.exe') $runtimeExe $pythonDaemon $ComPair $bridge.wire
                Write-Json (Join-Path $outputRoot 'windows-com\sequence-result.json') $comSequence
                if ($comSequence.status -eq 'FAIL') {
                    $missing.Add('COM20→COM21 attach/cmd/release/attach 序列實際失敗')
                } elseif ($comSequence.status -ne 'PASS') {
                    $missing.Add('COM20→COM21 attach/cmd/release/attach 序列證據不足')
                }
            } catch {
                $comSequence = [ordered]@{
                    status = 'EVIDENCE-INSUFFICIENT'
                    reason = $_.Exception.Message
                    com_pair = $ComPair
                }
                Write-Json (Join-Path $outputRoot 'windows-com\sequence-result.json') $comSequence
                $missing.Add("Windows COM 序列未完整執行：$($_.Exception.Message)")
            }
        }

        $activePair = $null
        $activeWire = $null
        $pidLog = $null
        if ($ComPair -and $selftest -and $selftest.diagnostic_ready -and $bridge) {
            $activePair = $ComPair
            $activeWire = $bridge.wire
            $pidLog = Join-Path $outputRoot 'b.pids'
        } elseif ($ComPair) {
            $missing.Add('B 輪未帶 COM pair：selftest 實際失敗或 COM bridge 未啟動')
        }
        $b = Invoke-Blackbox 'b-rust-win' $bStagingJunit (Join-Path $bundleBin 'serialwrap.exe') `
            $bundleDaemon $runtimeExe $Label $true $stagingResourceReport $bundleRoot `
            $activePair $activeWire $pidLog
        # B 輪 L1 產物（junit 與 resource-report）自 staging 複製到 OutDir\l1\windows\blackbox\。
        Get-ChildItem -LiteralPath $stagingL1Directory -File -ErrorAction SilentlyContinue | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $l1Directory $_.Name) -Force
        }
        if (Test-Path -LiteralPath $bJunit -PathType Leaf) {
            Copy-Item -LiteralPath $bJunit -Destination $bCompareJunit -Force
        }
    } else {
        $missing.Add('A 輪後釋放檢查失敗；依序 A/B 規則略過 B')
        $b = [ordered]@{ exit = 1; junit = $bJunit; stdout = $null; stderr = $null; l1_report = $null; skipped = $true }
    }

    if ($bridge) {
        try {
            $bridgeStop = Stop-WindowsComBridge $bridge $scratchRoot
        } catch {
            $bridgeStop = [ordered]@{ status = 'FAIL'; reason = $_.Exception.Message }
            if ($bridge.process -and -not $bridge.process.HasExited) {
                & taskkill.exe /PID $bridge.process.Id /T /F *> $null
                $null = $bridge.process.WaitForExit(10000)
            }
            if (Test-Path -LiteralPath $bridge.control -PathType Leaf) {
                Remove-Item -LiteralPath $bridge.control -Force -ErrorAction SilentlyContinue
            }
        }
        Write-Json (Join-Path $outputRoot 'windows-com\bridge-stop.json') $bridgeStop
        if ($bridgeStop.status -ne 'PASS') { $missing.Add('uart_stdio_bridge 未正常關閉') }
    }

    foreach ($requiredFile in @($aJunit, $bJunit, $resourceReport)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            $missing.Add("必要黑箱/L1 證據不存在：$requiredFile")
        }
    }

    $p4CaseNode = 'tests/contract_blackbox/test_cli_mgmt_daemon.py::test_autostart_l15_tx_once'
    $p4CaseOutcome = $null
    if (Test-Path -LiteralPath $resourceReport -PathType Leaf) {
        try {
            $l1Report = Get-Content -LiteralPath $resourceReport -Raw -Encoding UTF8 | ConvertFrom-Json
            $caseRows = @($l1Report.results | Where-Object { $_.nodeid -ceq $p4CaseNode })
            if ($caseRows.Count -eq 1) { $p4CaseOutcome = [string]$caseRows[0].outcome }
            elseif ($caseRows.Count -gt 1) { $missing.Add('B 輪 COM 凍結案例在 resource report 中重複') }
        } catch {
            $missing.Add("無法解析 B 輪 resource report：$($_.Exception.Message)")
        }
    }
    if ($ComPair) {
        if ($selftest -and $selftest.status -eq 'FAIL') {
            $comStatus = 'FAIL'
        } elseif ($comSequence -and $comSequence.status -eq 'FAIL') {
            $comStatus = 'FAIL'
        } elseif ($bridgeStop -and $bridgeStop.status -eq 'FAIL') {
            $comStatus = 'FAIL'
        } elseif ($p4CaseOutcome -in @('failed', 'error')) {
            $comStatus = 'FAIL'
        } elseif (-not $selftest -or $selftest.status -ne 'PASS' -or
                  -not $comSequence -or $comSequence.status -ne 'PASS' -or
                  -not $bridgeStop -or $bridgeStop.status -ne 'PASS' -or
                  $p4CaseOutcome -cne 'passed' -or -not (Test-Path -LiteralPath (Join-Path $outputRoot 'windows-com\bridge\wire.ndjson'))) {
            $comStatus = 'EVIDENCE-INSUFFICIENT'
            $missing.Add('Windows COM PASS 條件未全部具備；至少一個自測、序列、B 輪凍結案例或 wire 證據缺失')
        } else {
            $comStatus = 'PASS'
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
        windows_com_case = [ordered]@{ nodeid = $p4CaseNode; outcome = $p4CaseOutcome }
        windows_com_selftest = $selftest
        windows_com_sequence = $comSequence
        windows_com_bridge_stop = $bridgeStop
        evidence_insufficient = @($missing)
    }
    Write-Json (Join-Path $outputRoot 'windows-com.json') ([ordered]@{
        format = 'windows-com-evidence.v1'
        windows_com = $comStatus
        com_pair = $ComPair
        selftest = $selftest
        selftest_paths = if ($selftest) { [ordered]@{ local_out = $selftest.local_out; evidence_copy = $selftest.evidence_copy; report = $selftest.report } } else { $null }
        sequence = $comSequence
        bridge_stop = $bridgeStop
        p4_case = [ordered]@{ nodeid = $p4CaseNode; outcome = $p4CaseOutcome }
        bridge_wire = Join-Path $outputRoot 'windows-com\bridge\wire.ndjson'
    })
    Write-Json (Join-Path $outputRoot 'report.json') $report
    Write-Output (Join-Path $outputRoot 'report.json')
    if ($a.exit -ne 0 -or $b.exit -ne 0 -or -not $releaseCheck.ok -or
        ($ComPair -and $comStatus -ne 'PASS') -or $missing.Count -gt 0) { exit 1 }
} finally {
    if ($bridge -and -not $bridgeStop) {
        try { $null = Stop-WindowsComBridge $bridge $scratchRoot } catch {
            if ($bridge.process -and -not $bridge.process.HasExited) {
                & taskkill.exe /PID $bridge.process.Id /T /F *> $null
                $null = $bridge.process.WaitForExit(10000)
            }
            if (Test-Path -LiteralPath $bridge.control -PathType Leaf) {
                Remove-Item -LiteralPath $bridge.control -Force -ErrorAction SilentlyContinue
            }
        }
    }
    # 診斷輸出與 Windows dev bundle 必須在 staging 刪除前複製回 OutDir（失敗路徑亦然）。
    # 複製失敗（例如 bundle 尚未組完）只記錄警告，不得覆蓋原錯誤；scratch 一定要刪除（巢狀 finally）。
    try {
        Copy-BlackboxDiagnostics $stagingRepo $bundleRoot $outputRoot
    } catch {
        Write-Warning "診斷複製失敗（不覆蓋原錯誤）：$($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
