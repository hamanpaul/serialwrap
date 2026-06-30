## ADDED Requirements

### Requirement: Windows 單一執行檔建置
專案 SHALL 提供以 PyInstaller 將 `serialwrapd` 與 `serialwrap` 打成單一 `.exe` 的建置流程（one-file），並 MUST 內嵌 `sw_core/assets`（經 `importlib.resources` 於 runtime 取用）。SHALL 附 Windows 建置腳本（`scripts/build_windows.ps1`）。產物 `serialwrapd.exe` MUST 可免裝 Python 直接雙擊／命令列前景跑成 daemon。

#### Scenario: 建置產出單一執行檔
- **WHEN** 在 Windows 執行 `scripts/build_windows.ps1`
- **THEN** 於 `dist/` 產出 `serialwrapd.exe` 與 `serialwrap.exe`，兩者皆為單檔且內含 `sw_core/assets`

#### Scenario: exe 免裝 Python 跑起 daemon
- **WHEN** 在未安裝 Python 的環境執行 `serialwrapd.exe`
- **THEN** daemon 前景啟動、綁定 TCP endpoint，`serialwrap.exe session list` 連得上
