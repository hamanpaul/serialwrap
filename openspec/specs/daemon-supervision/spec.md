# daemon-supervision Specification

## Purpose
TBD - created by archiving change install-flow-systemd-pipx. Update Purpose after archive.
## Requirements
### Requirement: 三種監管模式與單一事實來源
系統 SHALL 支援 `systemd-user`（預設）、`systemd-system`（`--system`）、`on-demand`（降級）三種監管模式，並以 `config.yaml` 的 `supervision_mode` 為單一事實來源。

#### Scenario: setup 落地模式
- **WHEN** `serialwrap setup` 決定監管模式
- **THEN** `~/.config/serialwrap/config.yaml`（或系統 config）寫入 `supervision_mode`，後續 CLI/工具皆依此判斷

### Requirement: auto-spawn gate 杜絕雙 daemon
所有自動 spawn 路徑（CLI lazy-start、`minicom_router` AUTO_START）SHALL 先讀 `supervision_mode`：systemd 模式下 MUST NOT 自動 spawn。

#### Scenario: systemd 模式不自動 spawn
- **WHEN** `supervision_mode` 為 systemd，且 daemon 未在跑時執行 `serialwrap session list`
- **THEN** CLI 不自行 spawn daemon，回明確錯誤並提示 `serialwrap service start`（或 `systemctl --user start serialwrap`）

#### Scenario: on-demand 模式維持自動 spawn
- **WHEN** `supervision_mode` 為 on-demand，且 daemon 未在跑時連線
- **THEN** 沿用既有機制自動 spawn daemon 並以 `SingletonLock` 防雙開

### Requirement: systemd user unit
`systemd-user` 模式 SHALL 安裝 `~/.config/systemd/user/serialwrap.service`（`ExecStart=%h/.local/bin/serialwrapd`、`Restart=on-failure`），並啟用 linger 以開機自啟。

#### Scenario: crash 自動拉回
- **WHEN** user unit 已啟用且 daemon 進程非正常結束
- **THEN** systemd 依 `Restart=on-failure` 重啟 daemon，重啟後重新認線、`state.json` 的 RELEASED 仍被尊重

### Requirement: systemd system unit
`systemd-system` 模式 SHALL 安裝 `/etc/systemd/system/serialwrap.service`，以專屬 `serialwrap` 服務帳號（在 `dialout`）執行，socket 權限允許其他使用者連線。

#### Scenario: 全機共用服務
- **WHEN** 以 `serialwrap setup --system` 安裝並啟動
- **THEN** 系統 daemon 以服務帳號常駐、開機自啟，`/run/serialwrap/serialwrapd.sock` 為 660 + 群組可連

### Requirement: unit 不得使用阻擋 /dev 的沙箱
產生的 systemd unit MUST NOT 含 `PrivateDevices`、`DeviceAllow` 等會隱藏/阻擋 `/dev/ttyUSB*` 的指令。

#### Scenario: 服務仍能開 serial
- **WHEN** 檢視產生的 unit 內容並啟動服務
- **THEN** unit 不含阻擋 /dev 的沙箱指令，daemon 能正常開啟 `/dev/ttyUSB*`

### Requirement: service 子命令包裝 systemctl
`serialwrap service {start|stop|restart|status}` SHALL 依 `supervision_mode` 轉成對應 `systemctl [--user]`；systemd 模式下 `serialwrap daemon stop` SHALL 重導到 `service stop`。

#### Scenario: 統一的服務操作
- **WHEN** 在 systemd-user 模式執行 `serialwrap service restart`
- **THEN** 實際執行 `systemctl --user restart serialwrap`，使用者無需自行指定範圍

### Requirement: on-demand 降級備援
無 systemd（或未啟用）時，`setup` SHALL 落 `on-demand` 模式並維持現有 spawn-on-connect 行為。

#### Scenario: 無 systemd 平台
- **WHEN** 平台無 systemd 且無法啟用，執行 `serialwrap setup`
- **THEN** `supervision_mode` 設為 `on-demand`，後續 CLI/minicom 連不到 daemon 時自動 spawn + `SingletonLock`

