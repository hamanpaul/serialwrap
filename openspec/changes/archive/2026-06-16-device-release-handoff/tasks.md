## 1. 狀態與資料結構

- [x] 1.1 `SessionRuntime` 新增 `released_by` / `released_at` / `released_reason` 欄位；確立 `RELEASED` 狀態字串
- [x] 1.2 `SessionManager` 新增衍生集合 `_released_by_ids`（source of truth 仍在 session 欄位）

## 2. 單點 re-attach guard（TDD）

- [x] 2.1 寫測試：released by-id 時 `_spawn_attach` 直接 return（分別經 `clear_session` / `update_devices` / `bootstrap_attach` 觸發都不搶回）
- [x] 2.2 在 `_spawn_attach(by_id)` 最前面加入 released guard，令既有測試 2.1 轉綠

## 3. clean-slate detach

- [x] 3.1 寫測試：`_detach_session_locked(drop_consoles=True)` 關 FD、丟棄所有 console、`retained_consoles` 為 None（不 stash）
- [x] 3.2 `_detach_session_locked` 新增 `drop_consoles` 參數（預設 False，維持既有行為）

## 4. release / attach 核心方法（TDD）

- [x] 4.1 寫測試：`release_device` → clean-slate + `RELEASED` + provenance；已 released 冪等回 `already_released`；不存在 selector 回 `SESSION_NOT_FOUND`
- [x] 4.2 實作 `SessionManager.release_device`（呼叫 clean-slate detach、設欄位、加入 `_released_by_ids`、persist、不 `_spawn_attach`）
- [x] 4.3 寫測試：`attach_device` → 清 released + re-attach + 新 primary console；by-id 不在 device 表回 `DEVICE_NOT_PRESENT`
- [x] 4.4 實作 `SessionManager.attach_device`（清欄位與集合、persist、`_spawn_attach` 收回）

## 5. 唯讀 idle 偵測 + attach 安全 guard（TDD）

- [x] 5.1 寫測試：`_probe_external_holder` 回外部持有者 pid 清單（mock `/proc` 或 `lsof`）；驗證不開 tty、不做 I/O
- [x] 5.2 實作 `_probe_external_holder(real_path)`
- [x] 5.3 寫測試：`attach_device` 外部仍持有時回 `DEVICE_STILL_HELD`（附 pids）；`force=true` 略過
- [x] 5.4 在 `attach_device` 接上安全 guard

## 6. 持久化（跨 daemon 重啟）

- [x] 6.1 寫測試：`_save_state` / `_load_state` 對 released round-trip；`_load_state` 還原後 `bootstrap_attach` 不搶回（舊 state 無 `released` 鍵時視為空）
- [x] 6.2 `_save_state` / `_load_state` 擴充 `released` map，並於 service start 流程確保 load 在 `bootstrap_attach` 之前

## 7. self_test RELEASED 回報（TDD）

- [x] 7.1 寫測試：`self_test` 在 RELEASED 下回 `released_by`/`released_at`/`reason`/`external_holder`/`reclaimable`/`recommended_action`（有/無持有者兩情境）
- [x] 7.2 `self_test` 加 RELEASED 分支；`to_public_dict` 補 `released_by`/`released_at`，`device.list` 標 `released`

## 8. RPC + CLI

- [x] 8.1 寫測試：`service.rpc("device.release", ...)` / `("device.attach", ...)` dispatch 與參數透傳
- [x] 8.2 `sw_core/service.py` rpc dispatch 新增 `device.release` / `device.attach`
- [x] 8.3 `sw_core/cli.py` `device` subcommand 新增 `release` / `attach`（`--selector` / `--source` / `--reason` / `--force`）並串 `_run_rpc`
- [x] 8.4 CLI smoke：help / argparse 解析正確

## 9. 對抗測試（adversarial，PRE-PR）

- [x] 9.1 release 期間並發 `clear` / `recover` / `update_devices` / 模擬 restart，確認 guard 不被任一路徑繞過、`_released_by_ids` 與持久化不漂移
- [x] 9.2 模擬 flasher 多次 open/close：`external_holder`/`reclaimable` 標註正確翻轉，且因不自動收回而無誤收回

## 10. 文件與政策

- [x] 10.1 `README.md` 補 `device release` / `device attach` 用法與 handoff 流程（R-18 docs 對齊）
- [x] 10.2 `CHANGELOG.md` `[Unreleased]` 補實作條目
- [x] 10.3 `python3 -m pytest -q tests/` 全綠（除既有 pre-existing 失敗）、`python3 -m policy_check --repo .` 通過

## 11. 實機測試（real-machine, PRE-PR HARD GATE）

- [x] 11.1 真實 FTDI（`/dev/ttyUSB1`，#54 同一顆）以隔離 worktree daemon 實測 handoff：serialwrap attach（持有 FD）→ `device release`（`lsof` 證實 FD 釋放、clean-slate）→ 外部程式以 `TIOCEXCL` 獨佔搶到 raw port（證實真的放手）→ self-test 回 `external_holder`/`wait_external_flash`、`device attach` 回 `DEVICE_STILL_HELD` → 外部離開後 `device attach` 收回、fresh console；co-work 下 release 拆掉 human console（`closed_consoles:2`）。**註：環境無 CC2674/fw.bin/`ocp-mcu-upgrade`，故以「外部獨佔開啟 raw port」取代實際燒錄；handoff 機制本身已在真實硬體驗證通過。**
