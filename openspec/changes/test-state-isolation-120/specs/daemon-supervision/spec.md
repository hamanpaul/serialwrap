# daemon-supervision（delta）

## MODIFIED Requirements

### Requirement: CLI endpoint 解析在 socket 失聯時的監管模式 fallback
`serialwrap` CLI 解析有效 endpoint 時，若選用的來源為 config.yaml 的 `socket_path` 且其為 unix socket 路徑、但不可連線（檔不存在或 connect 被拒），CLI SHALL 依 config.yaml 的 `supervision_mode` 推導 canonical endpoint（`systemd-system → /run/serialwrap/serialwrapd.sock`；`systemd-user` 與 `on-demand → XDG 預設 SOCKET_PATH`）並改連之；canonical endpoint 不可連或與原值相同時，SHALL 回原值使既有錯誤照常浮現。此 fallback MUST NOT 改寫 config.yaml（CLI 對 config 維持唯讀），且僅適用 unix socket（非 unix endpoint 直接沿用、不做此 fallback）。明確指定的 `--endpoint` 或 `--socket` SHALL 維持最高優先序、不被 fallback 覆蓋；`--socket` 的「明確指定」判準 MUST 為「命令列有無傳入該參數」（argparse `default=None` sentinel），MUST NOT 以「傳入值是否異於 import-time 預設值」判定——傳入值恰等於預設 `SOCKET_PATH` 時同樣視為明確指定，直接使用、不讀 config.yaml 也不做 fallback。未傳 `--socket` 時 SHALL 沿用 config.yaml → 預設 `SOCKET_PATH` 的既有優先序。

#### Scenario: config socket 失聯時依 systemd-system 推回系統 socket
- **WHEN** config.yaml `supervision_mode: systemd-system` 且 `socket_path` 指向已不存在的 socket，系統 daemon 實際 listen 在 `/run/serialwrap/serialwrapd.sock`，執行 `serialwrap session list`
- **THEN** CLI 偵測原 socket 不可連，改連 `/run/serialwrap/serialwrapd.sock` 成功並於 stderr 提示 config.yaml 指向失效 socket，且不改寫 config.yaml

#### Scenario: config socket 可連時不觸發 fallback
- **WHEN** config.yaml `socket_path` 指向可連線的 socket，且未傳 `--socket`
- **THEN** CLI 直接使用該 socket，不進行任何 fallback 探測或提示

#### Scenario: 明確指定 endpoint 不被 fallback 覆蓋
- **WHEN** 使用者以 `--endpoint` 或 `--socket` 指定目標
- **THEN** CLI 使用該明確值、不讀 config.yaml 也不做監管模式 fallback

#### Scenario: 傳入值恰等於預設值仍視為明確指定
- **WHEN** 使用者（或測試）以 `--socket <path>` 傳入恰等於當前 env 推導預設 `SOCKET_PATH` 的路徑，而 config.yaml 記錄另一個可連線的 live socket
- **THEN** CLI 連向傳入的 `<path>`，MUST NOT fallback 至 config.yaml 的 socket（隔離測試的 RPC 不得誤路由到 live daemon）

#### Scenario: canonical 也不可連時回原值浮現錯誤
- **WHEN** config `socket_path` 與依 `supervision_mode` 推導的 canonical endpoint 皆不可連
- **THEN** CLI 回原 `socket_path` 並讓既有 `SOCKET_ERROR` 照常回報，不吞錯
