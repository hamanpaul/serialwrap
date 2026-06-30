## ADDED Requirements

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
