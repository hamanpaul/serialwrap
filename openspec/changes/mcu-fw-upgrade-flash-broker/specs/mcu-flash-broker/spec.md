## ADDED Requirements

### Requirement: byte-transparent flash 端點 `/dev/ttyMCU`

serialwrap SHALL 提供一個常駐的 byte-transparent flash 端點（PTY），以穩定 symlink 命名（預設
`${RUN_DIR}/dev/ttyMCU`）。外部 flasher 開啟該端點後，serialwrap SHALL 在端點與「已認定的 MCU 線」之間
做 1:1 raw 雙向轉送：endpoint→device 方向 SHALL 原樣送出（跳過 `_consume_console_input` 行處理），
device→endpoint 方向 SHALL 原樣回傳。daemon SHALL 維持為 real device 的唯一 reader（不釋放 FD），
因此結構上不存在 two-reader race。轉送全程 SHALL 持續寫入 RAW WAL（TX/RX）。

#### Scenario: flasher 透過端點完成 byte-perfect 轉送

- **WHEN** 一條已認定的 MCU 線存在，外部程式開啟 `/dev/ttyMCU` 並送出含 `0x08`/`0x0A`/`0x0D`/`0x7F` 的二進位資料
- **THEN** real device 端收到的位元組與送出的完全一致（未被退格/斷行/行組合汙染）
- **AND** device 回傳的位元組原樣出現在端點上
- **AND** 該次轉送的 TX/RX 出現在 RAW WAL

#### Scenario: daemon 維持唯一 reader

- **WHEN** flash 端點正在轉送
- **THEN** daemon 未釋放 real device FD、仍是唯一讀者
- **AND** 其他 COM session 的讀寫不受影響

### Requirement: 非破壞性 sync-probe 自動認 MCU 線

開啟 flash 端點且偵測到端點有輸入（flasher 送出 bytes）時，serialwrap SHALL 以非破壞性 sync-probe
自動認線：候選集合 SHALL 為目前 attached 的 session **排除 `command_capable` console**；對每個候選、
每個 registry pattern 送出該 pattern 的非破壞 probe 並在短 timeout 內等待其 expected ACK；
**第一個**回正確 ACK 的候選 SHALL 被鎖定為 MCU 線並記錄命中的 family。

#### Scenario: 認出回 ACK 的 MCU 線

- **WHEN** 候選中有一條在 BSL、對 TI pattern probe（`0x55 0x55`）回 ACK（`0x00 0xCC`）
- **THEN** 該候選被鎖定為 MCU 線、family 記為對應 TI 家族
- **AND** flash bridge 對該線建立

#### Scenario: 排除 command_capable console

- **WHEN** 候選包含一個 `command_capable` 的 console session（DUT）
- **THEN** 該 console session 不在 probe 候選內，不會被送出 probe、不會被選為 MCU 線

### Requirement: 多候選都回 ACK SHALL 拒絕並要求明指

當 probe 後有**多於一條**候選回正確 ACK 時，serialwrap SHALL NOT 自動挑選，SHALL 回 `FLASH_AMBIGUOUS`
並列出所有命中（含 by-id / real_path / family），要求以 `--selector` / `--by-id` 明確指定。

#### Scenario: 兩條候選都 ACK

- **WHEN** 兩條候選都對某 pattern 回正確 ACK
- **THEN** 不建立 bridge、回 `FLASH_AMBIGUOUS`，列出兩條命中的識別資訊

### Requirement: 偵測不到 MCU SHALL 保持沉默並週期 re-probe

當沒有任何候選回 ACK 時，serialwrap SHALL NOT 回合成錯誤、SHALL NOT 對端點寫入任何資料，使外部
flasher 走其自身的 retry/timeout。沉默期間 serialwrap SHALL 週期性 re-probe，以便較晚進入 BSL 的 MCU
仍能在 flasher 的重試窗內被認出並建立 bridge。

#### Scenario: 開燒時 MCU 尚未進 BSL

- **WHEN** flasher 已開啟端點送出 sync，但此刻沒有候選回 ACK
- **THEN** serialwrap 不回錯誤、不污染端點，flasher 依其 retry/timeout 自行重試

#### Scenario: BSL 稍晚就緒仍能 latch

- **WHEN** flasher 仍在重試窗內，期間某候選進入 BSL 並開始對 probe 回 ACK
- **THEN** serialwrap 在週期 re-probe 時認出該線並建立 bridge，本次 flash 得以繼續

### Requirement: 可擴充的 MCU pattern registry

serialwrap SHALL 維護 per-family 的 MCU pattern registry，每筆含 family 名稱、probe 位元組、
expected ACK、baud 與 timeout。registry SHALL 預設含 TI CC2674/CC2652（probe `0x55 0x55` → ACK `0x00 0xCC`）。
每筆 probe SHALL 為非破壞性 sync 握手（不得含 erase/program 等破壞性操作）；載入時 SHALL 拒絕未通過
非破壞審核標記的項目。`/dev/ttyMCU` 在**只讀**存取（無端點輸入）時 SHALL 回傳目前支援家族清單與候選狀態文字；
另 SHALL 提供 `mcu patterns` / `mcu status` CLI/RPC 作為 canonical 查詢。

#### Scenario: cat 端點列出支援的 MCU

- **WHEN** 對 `/dev/ttyMCU` 只讀（如 `cat`）且未送出任何 bytes
- **THEN** 回傳支援家族清單（含預設 TI CC2674/CC2652）與目前候選 tty/分類，並 EOF

#### Scenario: 拒絕破壞性 probe 項目

- **WHEN** registry 載入一筆未標記為非破壞性審核的 probe 項目
- **THEN** 該項目被拒絕載入（不可用於 probe）

### Requirement: FLASHING 狀態、仲裁與自動恢復

認線成功後，目標 session SHALL 進入 `FLASHING` 狀態。`FLASHING` 期間對該 session 的 `cmd submit`
SHALL 回 `FLASHING_BUSY`；其他 COM session SHALL 不受影響；daemon SHALL 持續運作。flash 結束
（端點被關閉/hangup、閒置 timeout、或顯式結束）後，serialwrap SHALL 以 `_probe_external_holder` 確認
無外部持有，再經 `_spawn_attach` 自動恢復 console 至原本能力；恢復失敗 SHALL 停於 `ATTACHING` 並回報
明確 `last_error`。flash 期間既有的 human console SHALL 轉為唯讀快照（可見進度、不可注入）。

#### Scenario: FLASHING 期間拒絕 cmd submit

- **WHEN** session 處於 `FLASHING`，對其 `cmd submit`
- **THEN** 回 `FLASHING_BUSY`，命令不送出

#### Scenario: 其他 COM 不受影響

- **WHEN** 某 session 處於 `FLASHING`
- **THEN** 其他 COM session 仍可正常 `cmd submit` 與 console 操作

#### Scenario: flash 結束自動恢復 console

- **WHEN** flasher 關閉端點（hangup）或閒置 timeout
- **THEN** session 離開 `FLASHING`，經 holder 確認 + `_spawn_attach` 自動恢復至原 console 能力
- **AND** 恢復失敗時停於 `ATTACHING` 並帶明確 `last_error`

### Requirement: baud / framing 鏡射到 real device

flash bridge 期間，serialwrap SHALL 將端點 PTY slave 的 termios（baud、framing）鏡射到 real device；
無法取得時 SHALL fallback 使用命中 pattern 的 registry baud（TI 預設 115200），避免端點與實體線
baud 不一致。

#### Scenario: flasher 設定 baud 後鏡射

- **WHEN** flasher 對端點 `tcsetattr` 設定 115200
- **THEN** real device 的 baud 同步為 115200（或 fallback 至 registry baud）

### Requirement: 明指 command_capable console 的誤燒防護

serialwrap SHALL 對「以 `--selector` / `--by-id` 明確指定一個 `command_capable` console 作為 flash 目標」
的請求預設擋下並警告（避免燒到 console/DUT 線），僅在帶 `--force` 時覆寫。

#### Scenario: 明指 console 被擋

- **WHEN** 明確指定一個 `command_capable` console session 作為 flash 目標且未帶 `--force`
- **THEN** 拒絕並警告，不建立 bridge

#### Scenario: --force 覆寫

- **WHEN** 同上但帶 `--force`
- **THEN** 允許對該 session 進行 flash
