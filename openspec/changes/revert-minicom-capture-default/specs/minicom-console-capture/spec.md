## ADDED Requirements

### Requirement: broker minicom 自動 transcript 預設使用 minicom 原生 `-C`

當使用者未設定 `MINICOM_CAPTURE_MODE` 也未設定 `MINICOM_CAPTURE_WRAPPER` 時，`minicom_router.sh` 的自動 transcript SHALL 採 minicom 原生 `-C`（`minicom -D <vtty> -C <logfile>`）模式，使預設 `~/b-log/mini_<COM>_<timestamp>.log` 為**純序列擷取**——不得包含 minicom 自身的全螢幕 UI（顏色、狀態列、游標定位、字元集切換、選單/對話框）等控制序列。系統 SHALL NOT 在此預設下以 `script` wrapper 包裹 minicom。

此預設 SHALL NOT 對 target 自身輸出的位元組做任何過濾——target 經序列埠送出的 ANSI（如 `ls --color`、著色 prompt）仍 SHALL 被如實記錄；本 requirement 僅排除 minicom **自身** UI 的污染。

#### Scenario: 無 env 設定時預設走原生 -C
- **WHEN** 啟動 `minicom_router.sh` 且未設 `MINICOM_CAPTURE_MODE` 與 `MINICOM_CAPTURE_WRAPPER`
- **THEN** 傳給 minicom 的引數 SHALL 含 `-C <logfile>`，SHALL NOT 以 `script` 包裹，且產生的 `mini_<COM>_<ts>.log` 不含 `Script started` 標頭

#### Scenario: 預設擷取檔不含 minicom UI 控制序列
- **WHEN** 以預設模式擷取一段含 minicom 狀態列/選單重繪的工作階段
- **THEN** 產出的 log 檔 SHALL NOT 含 minicom 自身的全螢幕重繪/狀態列/Leave 對話框等 UI 控制序列

### Requirement: 擷取模式 precedence 與 opt-in 維持不變

`minicom_router.sh` SHALL 以下列 precedence 解析擷取模式，且本 change 僅變動「最終 fallback」一層：

1. 顯式 `MINICOM_CAPTURE_MODE`（`script`｜`minicom`｜`off`）最高優先；非法值 SHALL 報錯並以非零碼結束。
2. 其次為 legacy `MINICOM_CAPTURE_WRAPPER`：曾被設定為 `1` SHALL 等同 `script`、設定為其他值（如 `0`）SHALL 等同 `minicom`。
3. 兩者皆未設定時，最終 fallback SHALL 為 `minicom`（原生 `-C`）。

`script` 模式（顯式 `MINICOM_CAPTURE_MODE=script` 或 legacy `MINICOM_CAPTURE_WRAPPER=1`）SHALL 維持為**顯式 opt-in** 的全終端 transcript（`script -qef`）。使用者自帶 `-C`/`--capturefile` 時 SHALL NOT 重複注入 auto transcript。`off` 模式 SHALL 不建立任何 log、不使用 `script`、不注入 `-C`。

#### Scenario: 顯式 MODE=script 仍走全終端 transcript
- **WHEN** 設 `MINICOM_CAPTURE_MODE=script`
- **THEN** SHALL 以 `script -qef` 包裹 minicom 並產生全終端 transcript（行為與本 change 前一致）

#### Scenario: legacy WRAPPER=1 仍等同 script
- **WHEN** 設 `MINICOM_CAPTURE_WRAPPER=1` 且未設 `MINICOM_CAPTURE_MODE`
- **THEN** 解析出的模式 SHALL 為 `script`

#### Scenario: legacy WRAPPER=0 仍等同 minicom
- **WHEN** 設 `MINICOM_CAPTURE_WRAPPER=0` 且未設 `MINICOM_CAPTURE_MODE`
- **THEN** 解析出的模式 SHALL 為 `minicom`（原生 `-C`）

#### Scenario: 使用者自帶 -C 不重複注入
- **WHEN** 使用者在 minicom 引數自帶 `-C <file>` 或 `--capturefile`
- **THEN** SHALL NOT 額外注入 auto transcript（無重複 `-C`、無 auto `mini_*.log`）

#### Scenario: off 模式不擷取
- **WHEN** 設 `MINICOM_CAPTURE_MODE=off`
- **THEN** SHALL NOT 建立 log、SHALL NOT 以 `script` 包裹、SHALL NOT 注入 `-C`

#### Scenario: script 不可用時的顯式降級
- **WHEN** 模式解析為 `script`（顯式 MODE 或 WRAPPER=1）但系統無 `script` 指令
- **THEN** SHALL 印出 warning 並改為不帶 `-C` 直接執行 minicom（維持既有降級行為）
