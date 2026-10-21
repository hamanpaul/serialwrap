param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-ExcludedPath([string]$Path, [string]$ExcludedRoot) {
    $candidate = Get-NormalizedPath $Path
    $excluded = Get-NormalizedPath $ExcludedRoot
    return $candidate.Equals($excluded, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith($excluded + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith($excluded + '/', [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-Snapshot([string[]]$Roots, [string]$ExcludedRoot) {
    $entries = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($root in $Roots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
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
                if (Test-ExcludedPath $item.FullName $ExcludedRoot) { continue }
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

$resolvedExe = (Resolve-Path -LiteralPath $Exe).Path
if (-not (Test-Path -LiteralPath $resolvedExe -PathType Leaf)) {
    throw "Windows CLI executable 不存在：$Exe"
}
$exeDirectory = Split-Path -Parent $resolvedExe
$outputDirectory = Get-NormalizedPath $OutDir
$null = New-Item -ItemType Directory -Path $outputDirectory -Force
$tempRoot = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
$localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { [Environment]::GetFolderPath('LocalApplicationData') }
$roots = @($tempRoot, $exeDirectory, $localAppData) | Where-Object { $_ } | Select-Object -Unique

$beforeSnapshot = Get-Snapshot $roots $outputDirectory
$before = $beforeSnapshot.entries
$snapshotErrors = [System.Collections.Generic.List[string]]::new()
$snapshotErrors.AddRange([string[]]$beforeSnapshot.errors)
$runs = [System.Collections.Generic.List[object]]::new()
$hashes = [System.Collections.Generic.List[string]]::new()
$allExitZero = $true

for ($index = 1; $index -le 100; $index++) {
    $stdoutPath = Join-Path $outputDirectory "startup-$index.stdout"
    $stderrPath = Join-Path $outputDirectory "startup-$index.stderr"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $started = [DateTime]::UtcNow.ToString('o')
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

$afterSnapshot = Get-Snapshot $roots $outputDirectory
$after = $afterSnapshot.entries
$snapshotErrors.AddRange([string[]]$afterSnapshot.errors)
$newArtifacts = [System.Collections.Generic.List[string]]::new()
foreach ($path in $after.Keys) {
    if ($before.ContainsKey($path)) { continue }
    $entry = $after[$path]
    $extension = [System.IO.Path]::GetExtension([string]$entry.path)
    if ($entry.kind -eq 'directory' -or $extension.Equals('.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
        $extension.Equals('.dll', [System.StringComparison]::OrdinalIgnoreCase)) {
        $newArtifacts.Add([string]$entry.path)
    }
}

$sameOutput = $hashes.Count -eq 100 -and (@($hashes | Select-Object -Unique).Count -eq 1)
$exeHash = (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToLowerInvariant()
$ok = $allExitZero -and $sameOutput -and $newArtifacts.Count -eq 0 -and $snapshotErrors.Count -eq 0
$report = [ordered]@{
    runs = @($runs)
    exe_sha256 = $exeHash
    new_artifacts = @($newArtifacts | Sort-Object -Unique)
    snapshot_errors = @($snapshotErrors)
    ok = $ok
}
$reportPath = Join-Path $outputDirectory 'startup-100.json'
$json = ConvertTo-Json -InputObject $report -Depth 8
[System.IO.File]::WriteAllText($reportPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
Write-Output $reportPath
if (-not $ok) { exit 1 }
