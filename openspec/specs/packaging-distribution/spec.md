# packaging-distribution Specification

## Purpose
TBD - created by archiving change install-flow-systemd-pipx. Update Purpose after archive.
## Requirements
### Requirement: pipx + git+SHA 安裝
serialwrap SHALL 可透過 `pipx install "git+https://github.com/hamanpaul/serialwrap@<tag或SHA>"` 安裝，並把依賴隔離於 pipx 管理的 venv。

#### Scenario: 以 git URL 安裝
- **WHEN** 使用者執行 `pipx install "git+https://github.com/hamanpaul/serialwrap@<tag>"`
- **THEN** 安裝成功，`serialwrap` 與 `serialwrapd` 出現在使用者 PATH（`~/.local/bin`），且 PyYAML 裝在隔離 venv 內而非系統環境

### Requirement: console_scripts entry points
套件 SHALL 提供 `serialwrap`（→ `sw_core.cli:main`）與 `serialwrapd`（→ `sw_core.daemon:main`）兩個 console_scripts。

#### Scenario: entry points 可解析執行
- **WHEN** 安裝後執行 `serialwrap --help` 與 `serialwrapd --help`
- **THEN** 兩者皆可執行且分別由 `sw_core.cli:main` / `sw_core.daemon:main` 提供

### Requirement: 依賴與 Python 門檻宣告
`pyproject.toml` SHALL 宣告 `PyYAML>=6` 為唯一第三方 runtime 依賴，且 `requires-python>=3.10`。

#### Scenario: Python 版本不符即拒裝
- **WHEN** 在 Python <3.10 環境嘗試安裝
- **THEN** 安裝因 `requires-python` 不符而明確失敗（不留下半裝狀態）

### Requirement: 資產隨輪子打包
預設 profiles、minicom wrappers、agent skill 與關鍵 docs SHALL 隨輪子打包並可於 runtime 經 `importlib.resources` 取得。

#### Scenario: runtime 取得套件資產
- **WHEN** 安裝後查詢套件內預設 profiles
- **THEN** 可經 `importlib.resources` 讀到預設 profiles/minicom/skill 內容（不依賴原始 repo 路徑）

### Requirement: root serialwrapd.py 相容 shim
root `serialwrapd.py` SHALL 保留為薄 shim（`from sw_core.daemon import main`），相容既有以該檔啟動 daemon 的呼叫。

#### Scenario: 舊式啟動仍可用
- **WHEN** 執行 `python serialwrapd.py --socket <path> --lock <path>`
- **THEN** daemon 正常啟動，行為等同 `serialwrapd` entry point

### Requirement: Windows 單一執行檔建置
專案 SHALL 提供以 PyInstaller 將 `serialwrapd` 與 `serialwrap` 打成單一 `.exe` 的建置流程（one-file），並 MUST 內嵌 `sw_core/assets`（經 `importlib.resources` 於 runtime 取用）。SHALL 附 Windows 建置腳本（`scripts/build_windows.ps1`）。產物 `serialwrapd.exe` MUST 可免裝 Python 直接雙擊／命令列前景跑成 daemon。

#### Scenario: 建置產出單一執行檔
- **WHEN** 在 Windows 執行 `scripts/build_windows.ps1`
- **THEN** 於 `dist/` 產出 `serialwrapd.exe` 與 `serialwrap.exe`，兩者皆為單檔且內含 `sw_core/assets`

#### Scenario: exe 免裝 Python 跑起 daemon
- **WHEN** 在未安裝 Python 的環境執行 `serialwrapd.exe`
- **THEN** daemon 前景啟動、綁定 TCP endpoint，`serialwrap.exe session list` 連得上

