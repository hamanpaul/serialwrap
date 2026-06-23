# runtime-paths Specification

## Purpose
TBD - created by archiving change install-flow-systemd-pipx. Update Purpose after archive.
## Requirements
### Requirement: XDG 路徑解析（脫離 /tmp）
未設 env 覆寫時，user 範圍的 runtime/state/config/data SHALL 解析到 XDG 位置，且 socket/lock MUST NOT 預設落在 `/tmp`。

#### Scenario: user 範圍預設路徑
- **WHEN** 未設任何 `SERIALWRAP_*` env 啟動 daemon/CLI
- **THEN** socket/lock 在 `$XDG_RUNTIME_DIR/serialwrap`（缺則退 `$XDG_STATE_HOME/serialwrap/run`，不在 `/tmp`），`state.json`/`wal` 在 `$XDG_STATE_HOME/serialwrap`，profiles/`config.yaml` 在 `$XDG_CONFIG_HOME/serialwrap`

### Requirement: env 覆寫優先序最高
所有既有 `SERIALWRAP_*` 路徑 env SHALL 維持作用且優先序高於 XDG 預設。

#### Scenario: throwaway daemon 隔離跑法不受影響
- **WHEN** 設定 `SERIALWRAP_RUN_DIR`/`SERIALWRAP_STATE_DIR`/`SERIALWRAP_BY_ID_DIR` 啟動 daemon
- **THEN** 所有路徑一律以 env 值為準（既有 throwaway-daemon / CI 隔離跑法行為不變）

### Requirement: system 範圍固定路徑 + 有效 socket 記錄
`systemd-system` 模式 SHALL 使用固定系統路徑（socket `/run/serialwrap/serialwrapd.sock`、state `/var/lib/serialwrap`、config `/etc/serialwrap`），且 CLI SHALL 能由 `config.yaml` 解析到有效 socket 路徑。

#### Scenario: 他人 CLI 連到系統服務
- **WHEN** 在 `systemd-system` 模式下，另一使用者執行 `serialwrap session list`
- **THEN** CLI 由 `config.yaml` 取得有效 socket 路徑並連上系統 daemon（socket mode 660 + 群組允許）

### Requirement: /tmp→XDG 狀態遷移
首次 `setup` SHALL 在偵測到 legacy `/tmp/serialwrap/state.json` 且新 state 位置尚無檔案時，遷移狀態到新位置。

#### Scenario: 遷移既有狀態
- **WHEN** 首次 `serialwrap setup`，存在 legacy `/tmp/serialwrap/state.json` 而新 state 位置為空
- **THEN** sessions/alias/RELEASED map 被遷移到新 XDG/system state 位置，內容保留

