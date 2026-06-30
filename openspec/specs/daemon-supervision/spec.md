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
`serialwrap service {start|stop|restart|status}` SHALL 依 `supervision_mode` 轉成對應 `systemctl [--user]`；systemd 模式下 `serialwrap daemon stop` SHALL 重導到 `service stop`，且 `serialwrap daemon start` SHALL 重導到 `service start`（對稱於 stop，避免顯式 `daemon start` 繞過 unit 管理另起非託管 daemon 造成 two-reader）。systemd-system 的特權重導動作 SHALL 依 `--with-sudo` 決定直接執行或回報待跑的 sudo 指令。

#### Scenario: 統一的服務操作
- **WHEN** 在 systemd-user 模式執行 `serialwrap service restart`
- **THEN** 實際執行 `systemctl --user restart serialwrap`，使用者無需自行指定範圍

#### Scenario: systemd 模式 daemon start 重導 service start
- **WHEN** `supervision_mode` 為 systemd（user 或 system），執行 `serialwrap daemon start`
- **THEN** 重導到 `service start`（systemd-system 特權動作依 `--with-sudo`），回應含 `_routed_to: "service start"`，且不直接 spawn 非託管 daemon

#### Scenario: systemd 模式下 minicom AUTO_START 不再生 coexist
- **WHEN** `supervision_mode` 為 systemd，`minicom_router` 以 `AUTO_START_DAEMON=1` 觸發 `serialwrap daemon start`
- **THEN** 該 `daemon start` 依上述重導到 `service start`，不另起 `/tmp/...` 暫存 socket 的非託管 daemon

### Requirement: on-demand 降級備援
無 systemd（或未啟用）時，`setup` SHALL 落 `on-demand` 模式並維持現有 spawn-on-connect 行為。

#### Scenario: 無 systemd 平台
- **WHEN** 平台無 systemd 且無法啟用，執行 `serialwrap setup`
- **THEN** `supervision_mode` 設為 `on-demand`，後續 CLI/minicom 連不到 daemon 時自動 spawn + `SingletonLock`

### Requirement: daemon start 顯式啟動在 on-demand 模式冪等
`on-demand` 監管模式下，`serialwrap daemon start` SHALL 在 spawn 前先對目標 endpoint 做存活探測（`health.ping`）；若已有健康 daemon 回應，則 MUST NOT spawn 第二個行程，並回 `{ok: true, already_running: true}`（附現有 endpoint）。僅當探測不到健康 daemon 時才 spawn，並沿用 `SingletonLock` 防雙開。

#### Scenario: 已有健康 daemon 時冪等 no-op
- **WHEN** `supervision_mode` 為 `on-demand`，已有健康 daemon 在跑，再次執行 `serialwrap daemon start`
- **THEN** CLI 探測到既有 daemon，回 `{ok: true, already_running: true}`，不 spawn 第二個行程

#### Scenario: 無 daemon 時照常啟動
- **WHEN** `supervision_mode` 為 `on-demand`，目標 endpoint 無回應，執行 `serialwrap daemon start`
- **THEN** CLI spawn daemon、等待就緒並回 `{ok: true, pid: ...}`，由 `SingletonLock` 保證單例

### Requirement: CLI endpoint 解析在 socket 失聯時的監管模式 fallback
`serialwrap` CLI 解析有效 endpoint 時，若選用的來源為 config.yaml 的 `socket_path` 且其為 unix socket 路徑、但不可連線（檔不存在或 connect 被拒），CLI SHALL 依 config.yaml 的 `supervision_mode` 推導 canonical endpoint（`systemd-system → /run/serialwrap/serialwrapd.sock`；`systemd-user` 與 `on-demand → XDG 預設 SOCKET_PATH`）並改連之；canonical endpoint 不可連或與原值相同時，SHALL 回原值使既有錯誤照常浮現。此 fallback MUST NOT 改寫 config.yaml（CLI 對 config 維持唯讀），且僅適用 unix socket（非 unix endpoint 直接沿用、不做此 fallback）。明確指定的 `--endpoint` 或非預設 `--socket` SHALL 維持最高優先序、不被 fallback 覆蓋。

#### Scenario: config socket 失聯時依 systemd-system 推回系統 socket
- **WHEN** config.yaml `supervision_mode: systemd-system` 且 `socket_path` 指向已不存在的 socket，系統 daemon 實際 listen 在 `/run/serialwrap/serialwrapd.sock`，執行 `serialwrap session list`
- **THEN** CLI 偵測原 socket 不可連，改連 `/run/serialwrap/serialwrapd.sock` 成功並於 stderr 提示 config.yaml 指向失效 socket，且不改寫 config.yaml

#### Scenario: config socket 可連時不觸發 fallback
- **WHEN** config.yaml `socket_path` 指向可連線的 socket
- **THEN** CLI 直接使用該 socket，不進行任何 fallback 探測或提示

#### Scenario: 明確指定 endpoint 不被 fallback 覆蓋
- **WHEN** 使用者以 `--endpoint` 或非預設 `--socket` 指定目標
- **THEN** CLI 使用該明確值、不讀 config.yaml 也不做監管模式 fallback

#### Scenario: canonical 也不可連時回原值浮現錯誤
- **WHEN** config `socket_path` 與依 `supervision_mode` 推導的 canonical endpoint 皆不可連
- **THEN** CLI 回原 `socket_path` 並讓既有 `SOCKET_ERROR` 照常回報，不吞錯

