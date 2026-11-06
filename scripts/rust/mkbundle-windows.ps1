#requires -Version 5.1
<#
.SYNOPSIS
  建立 serialwrap Windows x86_64 正式 bundle（OD-78）。與 win-blackbox.ps1 的 dev bundle 完全分離。

.DESCRIPTION
  產出 <Out>\ 目錄（目錄名請用 serialwrap-<VERSION>-windows-x86_64，zip 的頂層即此名）：
    bin\serialwrap.exe、bin\serialwrapd.exe、bin\current-release.json
    versions\<V>\bin\*.exe、versions\<V>\py\（CPython 3.12.3 embeddable＋PyYAML/pyserial＋sw_core）、
    versions\<V>\release-manifest.json、versions\<V>\SHA256SUMS
  - runtime 為 python.org CPython 3.12.3 embeddable（SHA-256 見 win-python-3.12.3.sha256）；
    python312._pth 只含相對路徑（可搬移），sw_core 位於 py\sw_core（Rust install_layout 固定讀此處）。
  - 依賴以 win-bundle-requirements.txt 的 --require-hashes 安裝 wheel。
  - 不含 test-fakes、不含 com0com、不簽署。
  - 程式碼指紋比照 mkbundle.sh：cargo 期間改寫 crates\sw-cli\src\embedded_code_fingerprint.bin，結束或失敗都還原。

.PARAMETER Out
  輸出 bundle 根目錄（非空且無 .mkbundle-owner 者拒絕覆寫）。
.PARAMETER SourceRoot
  原始碼根目錄（預設為本腳本所在 repo）。
.PARAMETER PrebuiltBinDir
  已編好的 serialwrap.exe／serialwrapd.exe 目錄（略過 cargo；必須已蓋章程式碼指紋，腳本會以 --version 核對）。
  僅供無法在本機編譯時使用；release workflow 不使用。
.PARAMETER HostPython
  建置機 Python 3.12（跑 pip 與 manifest schema 驗證；需有 jsonschema）。
.PARAMETER PythonEmbedArchive
  已下載的 python-3.12.3-embed-amd64.zip（仍驗 SHA-256）；未給則從 python.org 下載。
.PARAMETER GitRevision / ContractRevision
  無 .git 的原始碼樹（例如 git archive 解出）時明確指定；有 .git 時省略。
.PARAMETER AllowDirty
  略過乾淨工作樹檢查（僅開發試建）。
.PARAMETER ZipPath
  指定則於驗證後輸出 zip。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$SourceRoot,
    [string]$PrebuiltBinDir,
    [string]$HostPython = 'python',
    [string]$PythonEmbedArchive,
    [string]$GitRevision,
    [string]$ContractRevision,
    [switch]$AllowDirty,
    [string]$ZipPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OwnerMarker = '.mkbundle-owner'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsRoot = (Resolve-Path (Join-Path $ScriptRoot '..\..')).Path
if (-not $SourceRoot) { $SourceRoot = $ToolsRoot }
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path

function Fail([string]$Message) { throw "mkbundle-windows.ps1: $Message" }

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$What) {
    # 以陣列參數呼叫外部程式並檢查 exit code；回傳 stdout 的字串陣列。
    $output = & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What 失敗 (exit $LASTEXITCODE)：$Exe $($Arguments -join ' ')" }
    return $output
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# ---- 版本與來源檢查 ----
$Version = (Get-Content -LiteralPath (Join-Path $SourceRoot 'VERSION') -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "VERSION 格式不合法：$Version" }
$cargoToml = Get-Content -LiteralPath (Join-Path $SourceRoot 'Cargo.toml') -Raw
if ($cargoToml -notmatch '(?ms)^\[workspace\.package\].*?^version\s*=\s*"([^"]+)"') { Fail 'Cargo.toml 找不到 [workspace.package] version' }
if ($Matches[1] -ne $Version) { Fail "Cargo.toml version $($Matches[1]) 與 VERSION $Version 不一致" }

if (-not $GitRevision -or -not $ContractRevision) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot '.git'))) {
        Fail '來源沒有 .git，請以 -GitRevision 與 -ContractRevision 明確指定'
    }
    $GitRevision = ([string](Invoke-Native git @('-C', $SourceRoot, 'rev-parse', 'HEAD') 'git rev-parse HEAD')).Trim()
    $ContractRevision = ([string](Invoke-Native git @('-C', $SourceRoot, 'rev-parse', 'HEAD:contracts') 'git rev-parse HEAD:contracts')).Trim()
    if (-not $AllowDirty) {
        $dirty = Invoke-Native git @('-C', $SourceRoot, 'status', '--porcelain', '--untracked-files=all') 'git status'
        if ($dirty) { Fail "正式 bundle 需乾淨工作樹：`n$($dirty -join "`n")" }
    }
}
if ($GitRevision -notmatch '^[0-9a-f]{40}$' -or $ContractRevision -notmatch '^[0-9a-f]{40}$') { Fail 'git revision 格式不合法' }

# 原始碼必須與 Linux 位元組一致（LF）；autocrlf 會讓 sw_core 與資產帶 CRLF。
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $SourceRoot 'sw_core') -Recurse -File -Include *.py, *.md, *.yaml, *.yml |
        Where-Object { $_.FullName -notmatch '__pycache__' }) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    if ([Array]::IndexOf($bytes, [byte]13) -ge 0) { Fail "$($file.FullName) 含 CR；請以 core.autocrlf=false 取出原始碼" }
}

# ---- 輸出目錄所有權 ----
$Out = [System.IO.Path]::GetFullPath($Out)
if (Test-Path -LiteralPath $Out) {
    $existing = @(Get-ChildItem -LiteralPath $Out -Force)
    if ($existing.Count -gt 0) {
        if (-not (Test-Path -LiteralPath (Join-Path $Out $OwnerMarker))) { Fail "輸出目錄非空且不是 mkbundle 建立：$Out" }
        $existing | Remove-Item -Recurse -Force
    }
}
$null = New-Item -ItemType Directory -Path $Out -Force
Set-Content -LiteralPath (Join-Path $Out $OwnerMarker) -Value 'mkbundle-windows.ps1 建立的輸出目錄' -Encoding UTF8

$VersionDir = Join-Path $Out "versions\$Version"
$PyDir = Join-Path $VersionDir 'py'
$VBin = Join-Path $VersionDir 'bin'
$RootBin = Join-Path $Out 'bin'
$null = New-Item -ItemType Directory -Path $PyDir, $VBin, $RootBin -Force
$Helper = Join-Path $ScriptRoot 'mkbundle_windows.py'

# ---- CPython 3.12.3 embeddable ----
$checksumFile = Join-Path $ScriptRoot 'win-python-3.12.3.sha256'
$expectedZipSha = ((Get-Content -LiteralPath $checksumFile -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$archiveName = 'python-3.12.3-embed-amd64.zip'
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("sw-winbundle-" + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $work -Force
try {
    $archive = Join-Path $work $archiveName
    if ($PythonEmbedArchive) {
        Copy-Item -LiteralPath $PythonEmbedArchive -Destination $archive -Force
    } else {
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.3/$archiveName" -OutFile $archive -UseBasicParsing
    }
    if ((Get-Sha256 $archive) -ne $expectedZipSha) { Fail "CPython embeddable SHA-256 不符（預期 $expectedZipSha）" }
    Expand-Archive -LiteralPath $archive -DestinationPath $PyDir -Force
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
$PyExe = Join-Path $PyDir 'python.exe'
if (-not (Test-Path -LiteralPath $PyExe)) { Fail 'embeddable 解壓後找不到 python.exe' }

# python312._pth 只用相對路徑：`.`＝py\（sw_core、serialwrapd.py 所在），Lib\site-packages＝依賴。
# 有 ._pth 時 -I／PYTHONPATH 都不影響 sys.path，故 bundle 搬到哪裡都載入同一份。
$pthFile = Get-ChildItem -LiteralPath $PyDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $pthFile) { Fail 'embeddable 缺少 python*._pth' }
$stdlibZip = ($pthFile.BaseName -replace '_pth$', '') + '.zip'
[System.IO.File]::WriteAllText($pthFile.FullName, ("$stdlibZip`n.`nLib\site-packages`n"), [System.Text.UTF8Encoding]::new($false))
$SitePackages = Join-Path $PyDir 'Lib\site-packages'
$null = New-Item -ItemType Directory -Path $SitePackages -Force

# ---- 依賴（雜湊釘死的 cp312 win_amd64 wheel）----
Invoke-Native $HostPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', '--no-compile',
    '--only-binary=:all:', '--require-hashes', '--platform', 'win_amd64', '--python-version', '3.12',
    '--implementation', 'cp', '--abi', 'cp312', '--target', $SitePackages,
    '-r', (Join-Path $ScriptRoot 'win-bundle-requirements.txt')) 'pip install' | Out-Host

# ---- sw_core、daemon 入口、VERSION ----
$copyArgs = @('/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD', '__pycache__', '/XF', '*.pyc')
& robocopy (Join-Path $SourceRoot 'sw_core') (Join-Path $PyDir 'sw_core') @copyArgs | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "robocopy sw_core 失敗 (exit $LASTEXITCODE)" }
Copy-Item -LiteralPath (Join-Path $SourceRoot 'serialwrapd.py') -Destination (Join-Path $PyDir 'serialwrapd.py') -Force
[System.IO.File]::WriteAllText((Join-Path $PyDir 'VERSION'), "$Version`n", [System.Text.UTF8Encoding]::new($false))
$reparse = Get-ChildItem -LiteralPath $PyDir -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
if ($reparse) { Fail "py 內含 symlink／junction：$($reparse[0].FullName)" }

# ---- runtime 隔離檢查：依賴與 sw_core 都必須由 bundle 載入 ----
Invoke-Native $PyExe @('-I', '-B', $Helper, 'isolation', '--py-dir', $PyDir) 'runtime 隔離檢查' | Out-Host

# ---- 程式碼指紋與 Rust 二進位 ----
$fingerprint = ([string](Invoke-Native $PyExe @('-I', '-B', $Helper, 'fingerprint', '--version-dir', $VersionDir) 'fingerprint' | Select-Object -Last 1)).Trim()
if ($fingerprint -notmatch '^[0-9a-f]{12}$') { Fail "程式碼指紋格式不符：$fingerprint" }

if ($PrebuiltBinDir) {
    $builtDir = (Resolve-Path -LiteralPath $PrebuiltBinDir).Path
} else {
    $stampFile = Join-Path $SourceRoot 'crates\sw-cli\src\embedded_code_fingerprint.bin'
    $placeholder = 'SWFP1:000000000000'
    try {
        [System.IO.File]::WriteAllText($stampFile, "SWFP1:$fingerprint", [System.Text.UTF8Encoding]::new($false))
        Push-Location $SourceRoot
        try {
            $env:SW_BUNDLE_VERSION = $Version
            # 不帶 --features test-fakes；與 rust.yml 相同 gnu host toolchain（由 RUSTUP_TOOLCHAIN 指定）。
            & cargo build --release --locked -p sw-cli -p sw-daemon
            if ($LASTEXITCODE -ne 0) { Fail "cargo build 失敗 (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }
    } finally {
        [System.IO.File]::WriteAllText($stampFile, $placeholder, [System.Text.UTF8Encoding]::new($false))
    }
    $targetRoot = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $SourceRoot 'target' }
    $builtDir = Join-Path $targetRoot 'release'
}
foreach ($name in 'serialwrap.exe', 'serialwrapd.exe') {
    if (-not (Test-Path -LiteralPath (Join-Path $builtDir $name))) { Fail "找不到 $builtDir\$name" }
    Copy-Item -LiteralPath (Join-Path $builtDir $name) -Destination (Join-Path $VBin $name) -Force
    Copy-Item -LiteralPath (Join-Path $builtDir $name) -Destination (Join-Path $RootBin $name) -Force
}

# 離開 manifest 的 CLI 仍須回報同一份指紋（複製到沒有 manifest 的目錄再問）。
$verifyDir = Join-Path ([System.IO.Path]::GetTempPath()) ("sw-fp-" + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $verifyDir -Force
try {
    Copy-Item -LiteralPath (Join-Path $VBin 'serialwrap.exe') -Destination (Join-Path $verifyDir 'serialwrap.exe')
    $env:SERIALWRAP_RELEASE_MANIFEST = $null
    Remove-Item Env:\SERIALWRAP_RELEASE_MANIFEST -ErrorAction SilentlyContinue
    $versionOut = ([string](& (Join-Path $verifyDir 'serialwrap.exe') --version 2>&1)).Trim()
} finally {
    Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue
}
if ($versionOut -ne "serialwrap $Version (code $fingerprint)") { Fail "離開 manifest 的 CLI 未回報預期指紋：'$versionOut'" }

# ---- 指標、manifest、內層 SHA256SUMS ----
[System.IO.File]::WriteAllText((Join-Path $RootBin 'current-release.json'),
    "{`"bundle_version`":`"$Version`",`"dir`":`"versions/$Version`"}`n", [System.Text.UTF8Encoding]::new($false))
Get-ChildItem -LiteralPath $VersionDir -Recurse -Force -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Invoke-Native $PyExe @('-I', '-B', $Helper, 'manifest', '--version-dir', $VersionDir, '--source-root', $SourceRoot,
    '--version', $Version, '--git-revision', $GitRevision, '--contract-revision', $ContractRevision,
    '--expected-fingerprint', $fingerprint) 'manifest' | Out-Host
Invoke-Native $HostPython @('-I', '-B', $Helper, 'validate', '--bundle-root', $Out, '--source-root', $SourceRoot,
    '--version', $Version) 'validate' | Out-Host

if ($ZipPath) {
    Invoke-Native $HostPython @('-I', '-B', $Helper, 'zip', '--bundle-root', $Out, '--output', $ZipPath) 'zip' | Out-Host
}
Write-Output $Out
