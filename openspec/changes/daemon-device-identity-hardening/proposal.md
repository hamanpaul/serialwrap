## Why

`serialwrapd` restart 後 COM 名與實體板的對應會被重新指派（#100，實機 `AC01QZT0`/`AQ00OAQ7` 對調），且同機可同時跑多個 daemon 造成靜默 two-reader（#101，RX 掉字、狀態污染），兩者皆源自 2026-06-29 同一次實機事件，且只能靠 user 手動 `ps` 才發現。本變更讓 COM↔板 對應確定性穩定、並讓 daemon 主動暴露多開狀態。

## What Changes

- **COM 編號改為依 `device_key`（by-id，by-path fallback）排序的確定性 rank**：startup 在 lock 內、spawn attach threads 之前一次配好 COM rank，消除現行「並發 attach 完成順序決定 COM0」的 race（#100 根因）。
- **rank 作用域限定 dynamic 自動偵測 session**：explicit YAML `targets` / `session.bind` 綁定的 COM 為權威、排除在 rank pool 外。
- **runtime hotplug 維持現有行為**：不同 by-id 的板繼承空出的 DETACHED/RELEASED 槽（`_attach_by_id` rebind 不改），active session COM 名 daemon 存活期間不變。
- **`session renumber` 已 defer 至 follow-up（#103）**（需以「拆 bridge → 改號 → 重 attach」重做，避免弄壞 attach 時以值捕捉的 bridge callback / flash state / lease reverse-link），本 PR 不含。
- **新增 daemon 多開被動偵測**：on-demand 掃 /proc 找其他 `serialwrapd` 與 tty 持有者，暴露到 `serialwrap doctor` 與 `serialwrap daemon status`，不自動 refuse/kill/退讓。

## Capabilities

### New Capabilities
- `com-identity-binding`: COM 編號確定性綁定到裝置 by-id（startup sorted rank、rank 作用域、runtime hotplug 沿用）。注：`session renumber` on-demand 重排已 defer 至 follow-up。
- `daemon-multi-open-detection`: daemon 被動偵測同機多開 / two-reader，並透過 doctor 與 daemon status 回報（含跨 uid 無權限時的明確降級）。

### Modified Capabilities
<!-- 無既有 spec 的 requirement 變更；本變更為兩個新 capability。 -->

## Impact

- **程式**：`sw_core/session_manager.py`（COM rank 分配、rank 作用域、pending lifecycle）、`sw_core/service.py`（`daemon status` 欄位、startup 兩入口）、`sw_core/cli.py`、`sw_core/doctor_cmd.py`（`_check_single_daemon`）、新增 module-level /proc 偵測 helper、`sw_core/device_watcher.py`（排序鍵語意確認）。（`session renumber` 已 defer，相關程式不在本 PR。）
- **對外契約**：`daemon status` 新欄位（`multi_open`/`foreign_holders`）、doctor 新檢查項 → README / `docs/serialwrap-spec.md` 對齊（R-18）。
- **持久化**：無新增持久化欄位（COM 確定性來自排序）。
- **不影響**：#94（另案除錯）、#84（另案移植）、WAL 格式、event engine、flash/MCU 路徑。
