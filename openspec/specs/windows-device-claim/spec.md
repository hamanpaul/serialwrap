# windows-device-claim Specification

## Purpose
TBD - created by archiving change windows-daemon-84. Update Purpose after archive.
## Requirements
### Requirement: Windows 原生 COM 列舉
Windows 上的 device source SHALL 以 registry `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` 列舉目前存在的 COM 埠，不模擬 `/dev/serial/by-id`。每個 COM 的穩定 key MUST 為 COM 名（如 `COM8`），開埠路徑 MUST 為 `\\.\COMx`。`DeviceWatcher` 的輪詢/diff/threading SHALL 留在平台中立 core，僅 `scan()` 由注入的平台 `DeviceSource` 提供。

#### Scenario: 列舉存在的 COM 埠
- **WHEN** 系統存在 COM3、COM4、COM8 且在 Windows 啟動 daemon
- **THEN** device source 由 `SERIALCOMM` 列出這些埠，每個以 COM 名為 key

#### Scenario: core diff 邏輯不分平台
- **WHEN** 注入測試用 `DeviceSource` 回報新增/移除埠
- **THEN** `DeviceWatcher` 產生與 POSIX 相同語意的 added/removed 事件（同一份 core 邏輯）

### Requirement: 藍牙 COM 永不接管
Windows device source SHALL 永遠排除藍牙 COM 埠：MUST 以 registry `HKLM\SYSTEM\CurrentControlSet\Enum\BTHENUM\**\Device Parameters\PortName` 收集藍牙埠並剔除，並 MUST 同時剔除 `SERIALCOMM` 中 value-name 含 `BthModem` 者。SHALL 另支援 config `windows.exclude_coms` 手動排除清單。被排除的埠 MUST NOT 被開啟或接管。

#### Scenario: 藍牙埠被排除
- **WHEN** `SERIALCOMM` 含 `\Device\BthModem0 → COM3`、`\Device\BthModem1 → COM4`、`\Device\Serial2 → COM8`
- **THEN** 接管候選僅含 COM8；COM3/COM4 被排除且全程不被開啟

#### Scenario: config 手動排除
- **WHEN** `windows.exclude_coms` 含 `COM8`
- **THEN** 即使 COM8 為非藍牙且閒置，亦不被接管

### Requirement: 閒置 COM 自動接管為 passthrough
Windows daemon SHALL 自動接管所有可獨佔開啟（閒置）的非藍牙 COM 埠：能開啟者 MUST 建立 dynamic session，預設 `platform=passthrough`（停在 ATTACHED、僅 RX/TX/WAL，不做 login/ready gating）；若 profile 明確綁定該 COM → template，則 SHALL 改用該 profile。無法開啟（被佔用）的埠 MUST 跳過，並於後續輪詢重試。

#### Scenario: 接管閒置埠
- **WHEN** COM8 非藍牙且未被其他程式佔用
- **THEN** daemon 開啟 COM8 並建立 passthrough session，可經 RPC `session list` 看到

#### Scenario: 被佔用埠跳過後重試
- **WHEN** COM8 當下被外部程式獨佔開啟
- **THEN** daemon 本輪跳過 COM8，下一輪輪詢偵測到閒置後再接管

#### Scenario: profile 綁定覆寫預設
- **WHEN** profiles 將 COM8 綁到某非 passthrough template
- **THEN** 接管 COM8 時改用該 template（走 prompt/login/ready），而非預設 passthrough

