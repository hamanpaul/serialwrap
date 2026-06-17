## Why

對掛在 serialwrap 下的 UART 做 MCU 韌體升級，目前只能用 `device release`（#54）把整個裝置交出去；
使用者要的是在 **daemon 持續 maintain tty** 下，讓 `ocp-mcu-upgrade` 直接走通，且不必記住底層是哪個
會漂移的 `/dev/ttyUSBx`（每次重燒用的轉接板甚至不一樣）。serialwrap 既有的「raw / passthrough」因為
（1）daemon 永遠在讀 real device、（2）console TX 只有 human owner 才 raw（其餘走行處理），無法承載
SBL 二進位協定。本變更補上 #54 當時列為「Desired」但未做的「pause bridge + byte-transparent 端點」。

## What Changes

- 新增 byte-transparent flash 端點 `/dev/ttyMCU`（常駐 PTY + 穩定 symlink）：flasher 開它即可，
  daemon 仍是 real device 唯一 reader（結構上免 two-reader race），全程保留 RAW WAL 證據。
- 新增**非破壞性 sync-probe** 自動認 MCU 線：以「行為」（BSL invoke 後會回 SBL ACK）判別，
  不依賴會漂的 `/dev/ttyUSBx` / by-id。
- 新增可擴充的 **MCU pattern registry**（預設 TI CC2674/CC2652：probe `0x55 0x55` → ACK `0x00 0xCC`）；
  `cat /dev/ttyMCU`（只讀）回支援家族清單與候選狀態，另有 `serialwrap mcu patterns` / `mcu status` CLI。
- 新增 session `FLASHING` 狀態與 raw bridge：命中後 ttyMCU ⇿ 目標 session real device 1:1 raw 雙向轉送
  （TX 跳過行處理）；期間該 session `cmd submit` 回 `FLASHING_BUSY`，其他 COM 不受影響；結束自動恢復 console。
- 安全防呆：detection 排除 `command_capable` console（DUT）；多候選都 ACK → `FLASH_AMBIGUOUS` 不自動挑；
  明指 console 需 `--force`；registry probe 限非破壞性。
- 非破壞 probe 偵測不到 MCU 時**不回合成錯誤**，保持沉默讓 flasher 自身 retry/timeout，並週期性 re-probe。

## Capabilities

### New Capabilities
- `mcu-flash-broker`: 在 daemon 持續運作下，提供 byte-transparent `/dev/ttyMCU` 端點 + 非破壞性 sync-probe
  自動認線 + 可擴充 pattern registry + `FLASHING` 狀態/raw bridge/自動恢復 + 誤燒防呆，讓外部 flasher
  （如 `ocp-mcu-upgrade`）原生走通 MCU 韌體升級。

### Modified Capabilities
<!-- 無 spec-level 既有需求變更：FLASHING 期間 cmd submit 回 FLASHING_BUSY 的行為併入新 capability spec。 -->

## Impact

- `sw_core/mcu_patterns.py`（新）：per-family sync/ack registry、非破壞不變式、`cat` 列表來源。
- `sw_core/flash_endpoint.py`（新）：`/dev/ttyMCU` PTY/symlink 生命週期、開啟分流（write→flash / read-only→列表）、
  sync-probe 偵測器（排除 console、逐候選逐 pattern、ambiguous 判定）。
- `sw_core/uart_io.py`：`UARTBridge` 加 raw/flash 旗標（TX 跳過 `_consume_console_input`）、baud termios 鏡射、
  flash bridge 路由；daemon 維持唯一 reader。
- `sw_core/session_manager.py`：新增 `FLASHING` 狀態與進出（沿用 release/attach 骨架）、`FLASHING_BUSY`、
  結束 `_probe_external_holder` + `_spawn_attach` 自動恢復。
- `sw_core/service.py` / `rpc.py`：新增 `mcu.*` RPC（patterns / status / flash begin/end 視設計）。
- `sw_core/cli.py`：新增 `mcu` subcommand（`patterns` / `status`）；device list 反查輔助。
- `tests/`：unit + 整合（假 PTY/loopback 假 MCU）；**強制真機 gate**（OCTOPUS/CC2674 實燒 `0x0`）。
- 文件：`README.md` / `CHANGELOG.md`（R-18 docs 對齊）；設計文件
  `docs/superpowers/specs/2026-06-17-mcu-fw-upgrade-flash-broker-design.md`。
- 對應 issue：`Closes #55`；底層基礎 #54（已交付）。
