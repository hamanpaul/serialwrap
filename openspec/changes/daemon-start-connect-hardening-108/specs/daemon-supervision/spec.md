## MODIFIED Requirements

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

## ADDED Requirements

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
