## Why

`minicom_router.sh` 的自動 transcript 預設於 commit `6df17a5`（PR #49，2026-06-01）從 minicom 原生 `-C` 翻成 `script -qef --color=on` 全終端 transcript，導致 `~/b-log/mini_<COM>_*.log` 從此夾帶大量 ANSI／控制碼（minicom 自身的全螢幕 UI：顏色、狀態列、游標定位、字元集切換、Leave 對話框）。實檔對照鐵證：早期 `mini_COM3_260611-095301.log` 0 個 ESC、純序列資料；近期 `mini_COM0_260627-114406.log` 含 185 個 ESC、首行為 `Script started … minicom … --color=on`。

當初翻成 `script` 宣稱是「避免 minicom 內建 `-C` 在 PTY/resize/高頻 RX 的 native crash 風險」，但經查證該 crash **查無任何 reproduction／issue／測試**（PR #49 body 自述「Issue Reference: N/A」），且本機 115200（約 11.5 KB/s）遠不足以觸發背壓掉包；真正的 minicom 卡頓/掉字根因是 serialwrap 端 stale/orphan console 累積（issue #76），與 minicom `-C` 無關（由 PR-B 處理）。

## What Changes

- 將 `minicom_router.sh` 自動 transcript 的**最終預設**由 `script` 改回 minicom 原生 `-C`（即無 `MINICOM_CAPTURE_MODE`／`MINICOM_CAPTURE_WRAPPER` 時的 fallback）。
- 保留 `MINICOM_CAPTURE_MODE=script|minicom|off` 與 legacy `MINICOM_CAPTURE_WRAPPER=1` 的既有語意：`script` 仍為**顯式 opt-in** 的全終端 transcript。
- 同步對齊 `README.md`／`docs/serialwrap-spec.md`，並**軟化**查無實證的「minicom native crash」措辭。
- **非破壞性**：預設 log 內容由「含 minicom UI 的全終端 transcript」改為「純序列 RX（仍含 target 自身輸出的 ANSI，如 `ls --color`）」；需要全終端錄製者顯式 `MINICOM_CAPTURE_MODE=script`。

## Capabilities

### New Capabilities
- `minicom-console-capture`: broker minicom wrapper 的自動 transcript 行為——擷取模式（`script`/`minicom`/`off`）、其 precedence、以及預設為 minicom 原生 `-C` 的乾淨序列擷取契約。

### Modified Capabilities
（無 requirement 層級的既有 capability 變更。）

## Impact

- `sw_core/assets/tools/minicom_router.sh`（`_resolve_capture_mode` 最終 fallback 一行）。
- `README.md`、`docs/serialwrap-spec.md`（docs 對齊 R-18；軟化 crash 措辭）。
- `CHANGELOG.md`（[Unreleased] 既有「改為 script」一條尚未出貨，直接修正而非疊加矛盾條目）。
- `tests/test_minicom_router.py`（重寫鎖死「預設=script」的測試、為 script-unavailable 測試補 `MODE=script`）。
- 無 daemon／RPC／序列埠 I/O 變更（不碰 `sw_core/uart_io.py`、`session_manager.py`）。
