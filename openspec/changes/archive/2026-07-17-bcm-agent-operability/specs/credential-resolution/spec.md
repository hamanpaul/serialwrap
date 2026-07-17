## ADDED Requirements

### Requirement: 帳密解析 SHALL 回報解析狀態與實際 env_file 路徑

`resolve_session_auth` SHALL 於回傳解析後的 `SessionAuth` 之外，額外回報一個解析狀態 `AuthResolution`，其 `reason` SHALL 為下列之一：

- `ok`：帳密成功解析（`user`/`pass` 非空），或 profile 合法未宣告帳密來源時的正常結果由 `not_configured` 表示。
- `env_file_missing`：profile 宣告了 `env_file`，但解析到的絕對路徑不存在。
- `env_file_unreadable`：env_file 存在但無法讀取（權限或格式錯誤）。
- `key_absent`：env_file 可讀，但缺 `user_env`/`pass_env` 指定的 key，或其值為空。
- `not_configured`：profile 未宣告任何帳密來源（`user_env`、`pass_env`、`env_file` 皆無）。

`AuthResolution` SHALL 帶「實際解析到的 env_file 絕對路徑」（當 profile 宣告 `env_file` 時），供示警訊息使用。既有僅取 `SessionAuth` 的呼叫端 SHALL 維持相容（以 tuple 解包或等值 helper 取得）。示警訊息 SHALL NOT 印出帳密值本身。

#### Scenario: env_file 不存在回報 env_file_missing

- **WHEN** profile 宣告 `env_file` 但其解析絕對路徑不存在
- **THEN** `AuthResolution.reason` 為 `env_file_missing`、帶該解析絕對路徑，`SessionAuth` 的 `user`/`pass` 為空

#### Scenario: env_file 缺 key 回報 key_absent

- **WHEN** env_file 存在可讀，但缺 `user_env`/`pass_env` 指定的 key（或值為空）
- **THEN** `AuthResolution.reason` 為 `key_absent`

#### Scenario: 未宣告帳密來源回報 not_configured

- **WHEN** profile 未宣告 `user_env`、`pass_env`、`env_file` 任一
- **THEN** `AuthResolution.reason` 為 `not_configured`

### Requirement: 宣告帳密但解析為空時 SHALL NOT 送空帳密並回 CREDENTIALS_UNRESOLVED

當板子出現 login prompt（`login_regex` 命中）、且該 session 的 profile **宣告了帳密來源**（`user_env`、`pass_env`、`env_file` 任一非空），但 `AuthResolution.reason` 不為 `ok`（帳密解析為空）時，login 流程 SHALL NOT 對 `Login:`/`Password:` 送出空字串，SHALL 中止本次登入嘗試，並將 session `last_error` set 為 `CREDENTIALS_UNRESOLVED`（與「板子尚未到 login prompt」的 `LOGIN_REQUIRED` 明確區分）。

進入 `CREDENTIALS_UNRESOLVED` 後，自動重探（reprobe）SHALL NOT 反覆送空帳密；此為明確終態，需操作者補帳密後手動 `attach`/`recover` 才重試。

profile **未宣告**帳密來源（`not_configured`）者，login 流程行為 SHALL 維持不變（現有 passwordless/auto-login 路徑不受影響）。

daemon SHALL 於進入此態時輸出一次清楚的警告（log 與 WAL），內容包含 env_file 實際解析的絕對路徑與失敗原因（`reason`），SHALL NOT 包含帳密值。

#### Scenario: 宣告 env_file 但檔缺失 → 不送空帳密、回 CREDENTIALS_UNRESOLVED

- **WHEN** profile 宣告 `env_file`，其解析路徑不存在，且板子出現 `Login:` prompt
- **THEN** login 流程不對 `Login:`/`Password:` 送出任何空字串
- **AND** session `last_error` 為 `CREDENTIALS_UNRESOLVED`
- **AND** log/WAL 有一次清楚警告，含 env_file 實際解析絕對路徑與原因

#### Scenario: CREDENTIALS_UNRESOLVED 為終態、不無限重試

- **WHEN** session 已進入 `CREDENTIALS_UNRESOLVED`
- **THEN** 後續自動 reprobe 不再送出空帳密登入嘗試

#### Scenario: 未宣告帳密來源不受影響

- **WHEN** profile 未宣告任何帳密來源（`not_configured`），走既有 passwordless/auto-login 路徑
- **THEN** login 流程行為與本變更前完全一致（不回 `CREDENTIALS_UNRESOLVED`）
