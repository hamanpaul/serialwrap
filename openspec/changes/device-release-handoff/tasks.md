## 1. 狀態與資料結構

- [ ] 1.1 `SessionRuntime` 新增 `released_by` / `released_at` / `released_reason` 欄位；確立 `RELEASED` 狀態字串
- [ ] 1.2 `SessionManager` 新增衍生集合 `_released_by_ids`（source of truth 仍在 session 欄位）

## 2. 單點 re-attach guard（TDD）

- [ ] 2.1 寫測試：released by-id 時 `_spawn_attach` 直接 return（分別經 `clear_session` / `update_devices` / `bootstrap_attach` 觸發都不搶回）
- [ ] 2.2 在 `_spawn_attach(by_id)` 最前面加入 released guard，令既有測試 2.1 轉綠

## 3. clean-slate detach

- [ ] 3.1 寫測試：`_detach_session_locked(drop_consoles=True)` 關 FD、丟棄所有 console、`retained_consoles` 為 None（不 stash）
- [ ] 3.2 `_detach_session_locked` 新增 `drop_consoles` 參數（預設 False，維持既有行為）

## 4. release / attach 核心方法（TDD）

- [ ] 4.1 寫測試：`release_device` → clean-slate + `RELEASED` + provenance；已 released 冪等回 `already_released`；不存在 selector 回 `SESSION_NOT_FOUND`
- [ ] 4.2 實作 `SessionManager.release_device`（呼叫 clean-slate detach、設欄位、加入 `_released_by_ids`、persist、不 `_spawn_attach`）
- [ ] 4.3 寫測試：`attach_device` → 清 released + re-attach + 新 primary console；by-id 不在 device 表回 `DEVICE_NOT_PRESENT`
- [ ] 4.4 實作 `SessionManager.attach_device`（清欄位與集合、persist、`_spawn_attach` 收回）

## 5. 唯讀 idle 偵測 + attach 安全 guard（TDD）

- [ ] 5.1 寫測試：`_probe_external_holder` 回外部持有者 pid 清單（mock `/proc` 或 `lsof`）；驗證不開 tty、不做 I/O
- [ ] 5.2 實作 `_probe_external_holder(real_path)`
- [ ] 5.3 寫測試：`attach_device` 外部仍持有時回 `DEVICE_STILL_HELD`（附 pids）；`force=true` 略過
- [ ] 5.4 在 `attach_device` 接上安全 guard

## 6. 持久化（跨 daemon 重啟）

- [ ] 6.1 寫測試：`_save_state` / `_load_state` 對 released round-trip；`_load_state` 還原後 `bootstrap_attach` 不搶回（舊 state 無 `released` 鍵時視為空）
- [ ] 6.2 `_save_state` / `_load_state` 擴充 `released` map，並於 service start 流程確保 load 在 `bootstrap_attach` 之前

## 7. self_test RELEASED 回報（TDD）

- [ ] 7.1 寫測試：`self_test` 在 RELEASED 下回 `released_by`/`released_at`/`reason`/`external_holder`/`reclaimable`/`recommended_action`（有/無持有者兩情境）
- [ ] 7.2 `self_test` 加 RELEASED 分支；`to_public_dict` 補 `released_by`/`released_at`，`device.list` 標 `released`

## 8. RPC + CLI

- [ ] 8.1 寫測試：`service.rpc("device.release", ...)` / `("device.attach", ...)` dispatch 與參數透傳
- [ ] 8.2 `sw_core/service.py` rpc dispatch 新增 `device.release` / `device.attach`
- [ ] 8.3 `sw_core/cli.py` `device` subcommand 新增 `release` / `attach`（`--selector` / `--source` / `--reason` / `--force`）並串 `_run_rpc`
- [ ] 8.4 CLI smoke：help / argparse 解析正確

## 9. 對抗測試（adversarial，PRE-PR）

- [ ] 9.1 release 期間並發 `clear` / `recover` / `update_devices` / 模擬 restart，確認 guard 不被任一路徑繞過、`_released_by_ids` 與持久化不漂移
- [ ] 9.2 模擬 flasher 多次 open/close：`external_holder`/`reclaimable` 標註正確翻轉，且因不自動收回而無誤收回

## 10. 文件與政策

- [ ] 10.1 `README.md` 補 `device release` / `device attach` 用法與 handoff 流程（R-18 docs 對齊）
- [ ] 10.2 `CHANGELOG.md` `[Unreleased]` 補實作條目
- [ ] 10.3 `python3 -m pytest -q tests/` 全綠（除既有 pre-existing 失敗）、`python3 -m policy_check --repo .` 通過

## 11. 實機測試（real-machine, PRE-PR HARD GATE）

- [ ] 11.1 真實 FTDI + CC2674：serialwrap attach → `device release` → `ocp-mcu-upgrade` 燒錄成功（`Return error code : 0x0`）→ `device attach` 收回、console/command 恢復（需 user 確認硬體就緒並給 go；未通過不得上 PR）
