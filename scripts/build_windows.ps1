# scripts/build_windows.ps1 — 在 Windows 產出 serialwrapd.exe / serialwrap.exe（one-file）
# 用法：
#   pwsh scripts/build_windows.ps1           # 增量建置
#   pwsh scripts/build_windows.ps1 -Clean    # 先清除 dist/ build/ 再建置
#
# 前置條件：Python 3.10+、pip 可用（建議在 venv 或 pipx shell 內執行）。
# 產出：dist\serialwrapd.exe、dist\serialwrap.exe

param(
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 切到 repo 根目錄（此腳本位於 scripts/ 子目錄）
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ($Clean) {
    Write-Host "清除舊建置產物..." -ForegroundColor Cyan
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

Write-Host "安裝 PyInstaller..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check "pyinstaller>=6"

Write-Host "執行 PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm serialwrap.spec

Write-Host "`n產出：" -ForegroundColor Green
Get-ChildItem dist\*.exe | Select-Object Name, @{Name="大小(bytes)";Expression={$_.Length}}

Write-Host "`n煙霧測試 serialwrap.exe --help：" -ForegroundColor Cyan
& ".\dist\serialwrap.exe" --help
