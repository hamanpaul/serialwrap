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

### Requirement: 平台感知的 RPC endpoint 預設
RPC endpoint 預設值 SHALL 為平台感知：POSIX MUST 維持既有 AF_UNIX 檔案路徑（`$XDG_RUNTIME_DIR/serialwrap/serialwrapd.sock`，缺則退 state 之下），Windows MUST 預設 `tcp://127.0.0.1:48700`，且 SHALL 可由 `--socket`／env 覆寫。daemon 啟動後 SHALL 將有效 endpoint 寫入 `config.yaml` 的 `socket_path`，使 CLI/agent 經既有 `_resolve_endpoint` 解析連上，無須額外參數。

#### Scenario: Windows 預設 endpoint
- **WHEN** 在 Windows 未設任何覆寫啟動 daemon
- **THEN** daemon 綁定並記錄 `tcp://127.0.0.1:48700`，`serialwrap session list` 不帶參數即連得上

#### Scenario: POSIX endpoint 不變
- **WHEN** 在 Linux 未設任何覆寫啟動 daemon
- **THEN** endpoint 維持既有 AF_UNIX 檔案路徑，行為與本變更前一致

#### Scenario: endpoint 可覆寫
- **WHEN** 以 `--socket tcp://127.0.0.1:50000` 啟動 Windows daemon
- **THEN** daemon 綁定該埠並寫入 `config.yaml`，CLI 解析到同一 endpoint

