# install-setup Specification

## Purpose
TBD - created by archiving change install-flow-systemd-pipx. Update Purpose after archive.
## Requirements
### Requirement: setup 物化資產到使用者可寫位置
`serialwrap setup` SHALL 把套件內預設 profiles、agent skill、minicom wrappers 物化到使用者可寫位置，且 MUST NOT 覆蓋使用者已存在的 profiles（除非 `--force`）。

#### Scenario: 首次物化
- **WHEN** 首次執行 `serialwrap setup`
- **THEN** profiles → `~/.config/serialwrap/profiles/`、skill 物化到 `~/.local/share/serialwrap/skill` 並 symlink `~/.agents/skills/serialwrap`、minicom wrapper → `~/.local/bin/serialwrap-minicom`

#### Scenario: 不覆蓋使用者改動
- **WHEN** 使用者已編輯 `~/.config/serialwrap/profiles/` 後再次執行 `serialwrap setup`（無 `--force`）
- **THEN** 既有 profiles 不被覆蓋；加 `--force` 才覆寫

### Requirement: setup 為冪等 reconciler，轉換先停舊再起新
`serialwrap setup` SHALL 為冪等 reconciler；模式變更時 SHALL 先停掉舊機制下的現役 daemon（釋放 tty FD）再起新機制，並確保轉換後恰好一個 daemon。

#### Scenario: 目標模式與舊相同
- **WHEN** 重跑 `setup` 且解析出的模式與 `config.yaml` 記錄相同
- **THEN** 冪等刷新（重物化資產、確保 unit enabled），不拆既有 daemon

#### Scenario: on-demand → systemd 轉換
- **WHEN** 舊為 on-demand、目標為 systemd 重跑 `setup`
- **THEN** 先停掉 on-demand daemon 釋放 `/dev/ttyUSB*`、（必要時）遷移 state、再裝/enable unit，最後驗證單一 daemon（無 two-reader）

### Requirement: 轉換/重裝的 flash/忙碌護欄
模式轉換前若偵測到任一 session 處於 `FLASHING` 或有進行中傳輸/前景命令，`setup` SHALL 中止並明確報錯（除非 `--force`）。

#### Scenario: 燒錄中拒絕切換
- **WHEN** 有 session 在 `FLASHING` 時執行會觸發轉換的 `setup`
- **THEN** setup 中止並報錯說明原因，不切斷燒錄；`--force` 才強制

### Requirement: sudo 邊界（不靜默 sudo）
需要 root 的動作（加入 `dialout`、寫 `/etc/wsl.conf`、安裝 `--system` unit）SHALL 預設只印出確切指令，僅在 `--with-sudo` 或互動同意時才代為執行。

#### Scenario: dialout 缺成員
- **WHEN** 執行使用者不在 `dialout` 群組
- **THEN** setup 印出 `sudo usermod -aG dialout $USER` 與重登提示，預設不自行 sudo

### Requirement: WSL systemd 引導
在 WSL 無 systemd 時，`setup` SHALL 引導啟用 `/etc/wsl.conf` 的 `[boot] systemd=true`（提示 `wsl --shutdown`），並在重啟前先以 on-demand 運作。

#### Scenario: WSL 尚未啟用 systemd
- **WHEN** 在未啟用 systemd 的 WSL2 執行 `serialwrap setup`
- **THEN** setup 引導寫入開機旗標 + 提示 `wsl --shutdown`，當下 `supervision_mode` 設 on-demand；重啟並重跑 setup 後切到 systemd-user

### Requirement: doctor 唯讀診斷
`serialwrap doctor` SHALL 唯讀檢查環境並對每項輸出可貼上的修復指令。

#### Scenario: 診斷輸出
- **WHEN** 執行 `serialwrap doctor`
- **THEN** 逐項回報 python 版本／PyYAML／`serialwrap`+`serialwrapd` 在 PATH／dialout 成員／systemd 與 unit 狀態／`supervision_mode`／socket 可連／`/dev/serial/by-id` 裝置／（WSL）systemd 旗標，並對失敗項印修復指令

### Requirement: legacy ~/.paul_tools 遷移
`setup` SHALL 偵測既有 `~/.paul_tools` 安裝並引導退役（停 legacy daemon、備份移除影子 `serialwrap`/`serialwrapd.py`/`minicom` symlink、提示移除 `PATH` 行）。

#### Scenario: 偵測 legacy 安裝
- **WHEN** 存在 `~/.paul_tools/serialwrap` 等 legacy 檔案時執行 `setup`
- **THEN** 警告 PATH 重複並提議退役 legacy（含備份），遷移其 `state.json`

### Requirement: minicom wrapper 以 command -v 解析真本體
minicom wrapper SHALL 以 `command -v minicom` 解析真 minicom 執行檔，MUST NOT 透過 PATH 順序 shadow `minicom`。

#### Scenario: 解析真 minicom
- **WHEN** 透過 `serialwrap-minicom` 開啟 broker console
- **THEN** wrapper 以 `command -v minicom` 找到真本體並執行，不依賴把自己排在 PATH 前面

