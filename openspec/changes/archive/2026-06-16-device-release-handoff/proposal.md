## Why

serialwrapd 對任何 attached session 會持續開啟並讀取真實 UART（`uart_io.py` 無 `TIOCEXCL`、`_loop` 持續 `os.read`），當外部工具（如 `ocp-mcu-upgrade`）要對同一 raw device 做 SBL 二進位協定的獨佔燒錄時，daemon 與 flasher 形成「同一 tty 兩個 reader」競爭，MCU fw upgrade 間歇失敗（#54）。現況唯一能乾淨放掉裝置的手段是整個 `daemon stop`（代價過高），而 `session clear` 被設計成 detach 後立即 re-attach，等於放不掉。需要一條 surgical、可恢復、且不被自身 re-attach 邏輯搶回的 device handoff 路徑。

## What Changes

- 新增 session 狀態 `RELEASED`：serialwrap 關閉 raw FD、clean-slate 清空 console，且刻意不再自動 attach，直到明確收回。
- 新增 RPC `device.release` / `device.attach` 與 CLI `serialwrap device release|attach --selector <COM>`（兩步式手動 handoff）。
- 在所有自動 attach 路徑的單一匯流點 `_spawn_attach(by_id)` 加 guard：released 的 by-id 一律略過（涵蓋 `clear_session` re-attach、`update_devices`、`bootstrap_attach`、recovery force re-attach）。
- released 狀態持久化（`_save_state` / `_load_state` 擴充），**跨 daemon 重啟**仍保持釋放，且在 `bootstrap_attach` 之前還原，避免重啟搶回燒錄中的裝置。
- 唯讀 idle 偵測標註（讀 `/proc` / `lsof`，不開 tty）：`self_test` 在 RELEASED 下回 `external_holder` / `reclaimable` / `recommended_action`，讓「被忘記的 release」可被看見、可安全收回。
- `device.attach` 預設安全：外部仍持有時回 `DEVICE_STILL_HELD`（`--force` 可略過），避免重回 two-reader race。
- 明確**不做**：自動偵測還原（auto-reclaim，會撞 flasher open/close race）、TTL lease、#55 原生 flash。

## Capabilities

### New Capabilities
- `device-handoff`: 把單一 session 綁定的 UART 裝置乾淨交給外部擁有者並可收回；含 RELEASED 狀態、release/attach RPC+CLI、單點 re-attach guard、跨重啟持久化、clean-slate console、attach 安全 guard。

### Modified Capabilities
- `session-selftest`: `self_test` 新增 RELEASED 狀態的回報（`released_by` / `released_at` / `reason` / `external_holder` / `reclaimable` / `recommended_action`）。

## Impact

- `sw_core/session_manager.py`：`SessionRuntime` 新欄位與 `RELEASED` 狀態；`_spawn_attach` guard；`_released_by_ids`；`_save_state`/`_load_state` 擴充；`_detach_session_locked` 新增 `drop_consoles`；`_probe_external_holder` helper；`self_test` 分支；`to_public_dict` 補欄位；`release_device` / `attach_device` 方法。
- `sw_core/service.py`：rpc dispatch 新增 `device.release` / `device.attach`。
- `sw_core/cli.py`：`device` subcommand 新增 `release` / `attach`。
- `sw_core/uart_io.py`：無需新邏輯（`stop(preserve_consoles=False)` 已可關 FD + console）。
- `tests/`：unit + 對抗測試；另有實機測試（FTDI + CC2674，PRE-PR hard gate）。
- 文件：`README.md` / `CHANGELOG.md`（R-18 docs 對齊）。
- 對應 issue：`Closes #54`；上層目標 #55。
