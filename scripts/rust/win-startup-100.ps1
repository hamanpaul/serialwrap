param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-ExcludedPath([string]$Path, [string[]]$ExcludedRoots) {
    $candidate = Get-NormalizedPath $Path
    foreach ($root in $ExcludedRoots) {
        if (-not $root) { continue }
        $excluded = Get-NormalizedPath $root
        if ($candidate.Equals($excluded, [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($excluded + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($excluded + '/', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Get-Snapshot([string[]]$Roots, [string[]]$ExcludedRoots, [switch]$RequireRoots) {
    $entries = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($root in $Roots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            # 判定用的根目錄（隔離目錄、exe 目錄）不存在即 fail-closed；ambient 根目錄不存在只略過
            if ($RequireRoots) { $errors.Add("$root：根目錄不存在") }
            continue
        }
        $pending = [System.Collections.Generic.Queue[string]]::new()
        $pending.Enqueue($root)
        while ($pending.Count -gt 0) {
            $current = $pending.Dequeue()
            try {
                $currentItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
                if (($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
                $items = @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)
            } catch {
                $errors.Add("$current：$($_.Exception.Message)")
                continue
            }
            foreach ($item in $items) {
                if (Test-ExcludedPath $item.FullName $ExcludedRoots) { continue }
                try {
                    $isDirectory = $item -is [System.IO.DirectoryInfo]
                    $isReparsePoint = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
                    $size = if ($isDirectory) { 0 } else { [int64]$item.Length }
                    $entries[$item.FullName] = [ordered]@{
                        path = $item.FullName
                        kind = if ($isDirectory) { 'directory' } else { 'file' }
                        size = $size
                        last_write_time_utc = $item.LastWriteTimeUtc.ToString('o')
                    }
                    if ($isDirectory -and -not $isReparsePoint) { $pending.Enqueue($item.FullName) }
                } catch {
                    $errors.Add("$($item.FullName)：$($_.Exception.Message)")
                }
            }
        }
    }
    return [pscustomobject]@{ entries = $entries; errors = @($errors) }
}

function Get-NewEntries($Before, $After) {
    $added = [System.Collections.Generic.List[object]]::new()
    foreach ($path in $After.Keys) {
        if (-not $Before.ContainsKey($path)) { $added.Add($After[$path]) }
    }
    return @($added)
}

$resolvedExe = (Resolve-Path -LiteralPath $Exe).Path
if (-not (Test-Path -LiteralPath $resolvedExe -PathType Leaf)) {
    throw "Windows CLI executable 不存在：$Exe"
}
$exeDirectory = Split-Path -Parent $resolvedExe
$outputDirectory = Get-NormalizedPath $OutDir
$null = New-Item -ItemType Directory -Path $outputDirectory -Force

# OD-25（2026-10-27）：判定對象是 CLI 造成的新增（規格 serialwrap-rust-spec-final.md:132）。
# 100 次執行的子行程 TEMP／TMP／LOCALAPPDATA 指向本次專用、執行前為空的隔離目錄；隔離目錄內任何新增
# （檔案或目錄，不限副檔名）即 FAIL。exe 所在目錄任何新增目錄／exe／dll 即 FAIL。真實 %LOCALAPPDATA%
# （含其下 %TEMP%）只記錄於 ambient_changes、不參與 ok 判定（不涵蓋以 known-folder API 取路徑、無視環境變數的解壓器）。
$ambientTemp = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$ambientLocalAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [Environment]::GetFolderPath('LocalApplicationData') }
$ambientRoots = @($ambientTemp, $ambientLocalAppData) | Where-Object { $_ } | Select-Object -Unique
$isolatedRoot = Get-NormalizedPath (Join-Path ([System.IO.Path]::GetTempPath()) ("sw-startup100-iso-" + [guid]::NewGuid().ToString('N')))
$isolatedLocalAppData = Join-Path $isolatedRoot 'LocalAppData'
$isolatedTemp = Join-Path $isolatedLocalAppData 'Temp'
$null = New-Item -ItemType Directory -Path $isolatedTemp -Force
$isolatedEnv = [ordered]@{ TEMP = $isolatedTemp; TMP = $isolatedTemp; LOCALAPPDATA = $isolatedLocalAppData }

$snapshotErrors = [System.Collections.Generic.List[string]]::new()
$ambientSnapshotErrors = [System.Collections.Generic.List[string]]::new()
$isolatedBefore = Get-Snapshot @($isolatedRoot) @() -RequireRoots
$exeBefore = Get-Snapshot @($exeDirectory) @($outputDirectory, $isolatedRoot) -RequireRoots
$ambientBefore = Get-Snapshot $ambientRoots @($outputDirectory, $isolatedRoot, $exeDirectory)
$snapshotErrors.AddRange([string[]]$isolatedBefore.errors)
$snapshotErrors.AddRange([string[]]$exeBefore.errors)
$ambientSnapshotErrors.AddRange([string[]]$ambientBefore.errors)
# 隔離目錄執行前只含本腳本建立的 LocalAppData 與 LocalAppData\Temp 兩層空目錄
if ($isolatedBefore.entries.Count -ne 2) {
    $snapshotErrors.Add("$isolatedRoot：執行前隔離目錄不為空（$($isolatedBefore.entries.Count) 項）")
}
$runs = [System.Collections.Generic.List[object]]::new()
$hashes = [System.Collections.Generic.List[string]]::new()
$allExitZero = $true

$savedEnv = @{}
foreach ($name in $isolatedEnv.Keys) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    foreach ($name in $isolatedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $isolatedEnv[$name], 'Process') }
    for ($index = 1; $index -le 100; $index++) {
        $stdoutPath = Join-Path $outputDirectory "startup-$index.stdout"
        $stderrPath = Join-Path $outputDirectory "startup-$index.stderr"
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
        $started = [DateTime]::UtcNow.ToString('o')
        # Start-Process 重導向時以 UseShellExecute=false 建立子行程，環境取自本行程（即上方隔離值）
        $process = Start-Process -FilePath $resolvedExe -ArgumentList @('--version') -WorkingDirectory $exeDirectory `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $processHandle = $process.Handle
        $process.WaitForExit()
        $ended = [DateTime]::UtcNow.ToString('o')
        $exitCode = [int]$process.ExitCode
        $process.Dispose()
        $stdoutHash = (Get-FileHash -LiteralPath $stdoutPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $hashes.Add($stdoutHash)
        if ($exitCode -ne 0) { $allExitZero = $false }
        $runs.Add([ordered]@{
            index = $index
            started_at = $started
            finished_at = $ended
            exit_code = $exitCode
            stdout_sha256 = $stdoutHash
        })
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
} finally {
    foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process') }
}

$isolatedAfter = Get-Snapshot @($isolatedRoot) @() -RequireRoots
$exeAfter = Get-Snapshot @($exeDirectory) @($outputDirectory, $isolatedRoot) -RequireRoots
$ambientAfter = Get-Snapshot $ambientRoots @($outputDirectory, $isolatedRoot, $exeDirectory)
$snapshotErrors.AddRange([string[]]$isolatedAfter.errors)
$snapshotErrors.AddRange([string[]]$exeAfter.errors)
$ambientSnapshotErrors.AddRange([string[]]$ambientAfter.errors)

$isolatedNew = @(Get-NewEntries $isolatedBefore.entries $isolatedAfter.entries | ForEach-Object { [string]$_.path })
$newArtifacts = [System.Collections.Generic.List[string]]::new()
foreach ($entry in (Get-NewEntries $exeBefore.entries $exeAfter.entries)) {
    $extension = [System.IO.Path]::GetExtension([string]$entry.path)
    if ($entry.kind -eq 'directory' -or $extension.Equals('.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
        $extension.Equals('.dll', [System.StringComparison]::OrdinalIgnoreCase)) {
        $newArtifacts.Add([string]$entry.path)
    }
}
$ambientChanges = @(Get-NewEntries $ambientBefore.entries $ambientAfter.entries | ForEach-Object { [string]$_.path })

$isolatedCleanupError = $null
try {
    Remove-Item -LiteralPath $isolatedRoot -Recurse -Force -ErrorAction Stop
} catch {
    $isolatedCleanupError = "$isolatedRoot：$($_.Exception.Message)"
}

$sameOutput = $hashes.Count -eq 100 -and (@($hashes | Select-Object -Unique).Count -eq 1)
$exeHash = (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToLowerInvariant()
$ok = $allExitZero -and $sameOutput -and $newArtifacts.Count -eq 0 -and $isolatedNew.Count -eq 0 -and
    $snapshotErrors.Count -eq 0
$report = [ordered]@{
    runs = @($runs)
    exe_sha256 = $exeHash
    isolated_root = $isolatedRoot
    isolated_env = $isolatedEnv
    isolated_new = @($isolatedNew | Sort-Object -Unique)
    new_artifacts = @($newArtifacts | Sort-Object -Unique)
    snapshot_errors = @($snapshotErrors)
    ambient_changes = @($ambientChanges | Sort-Object -Unique)
    ambient_snapshot_errors = @($ambientSnapshotErrors)
    isolated_cleanup_error = $isolatedCleanupError
    ok = $ok
}
$reportPath = Join-Path $outputDirectory 'startup-100.json'
$json = ConvertTo-Json -InputObject $report -Depth 8
[System.IO.File]::WriteAllText($reportPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Output $reportPath
if (-not $ok) { exit 1 }
