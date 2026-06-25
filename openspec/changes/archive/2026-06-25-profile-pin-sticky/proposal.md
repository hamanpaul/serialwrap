## Why

動態偵測（無 YAML explicit target）的 session，其 profile 由 daemon 啟動時 probe 決定、**每次重啟都重新偵測**，故會隨當下 UART 狀態漂移（實證：2026-06-25 本機 COM0 因板子吐 log 蓋掉 prompt probe，從 `prpl-template:COM0` 漂成 `others-template:COM0`、`command_capable:false`，agent 無法下命令）。根因是 `state.json` 從不持久化 detected profile，且現有 CLI（`bind`/`attach`/`clear`）都改不了既有 session 的 profile。

## What Changes

- 新增 **explicit pin**：`session pin --selector <COM|alias|sid|by-id|by-path> --profile <name>` / `session unpin`，把 device_key→profile 寫入 `state.json` 的 `profile_pins`，attach 時最高優先、繞過偵測。
- 新增 **detected sticky**：偵測成功且**達 READY** 後，把 device_key→profile 寫入 `state.json` 的 `profile_detected`，重啟沿用；偵測 fallback 與「未達 READY」皆不記，使漂移自我收斂。
- `_attach_by_id_dynamic` 改為**四層優先序**選 template：`pin` > `sticky` > `detect` > `fallback`；pin/sticky 命中時跳過 probe。
- 新增 `SessionRuntime.profile_source` 欄位（`pin`/`sticky`/`detected`/`fallback`/`yaml-target`），兼作可觀測性（`session list` 顯示）、sticky 寫入判斷、explicit-target 判斷三用。
- device_key 採與既有 binding 一致的穩定鍵；同款晶片 by-id 碰撞時以 by-path 為準。
- 新增 RPC `session.pin` / `session.unpin`；錯誤碼 `UNKNOWN_PROFILE`、`PROFILE_IS_EXPLICIT`。
- 同步 `README.md`/`docs/**`、`CHANGELOG.md`、測試。
- 無 BREAKING：舊 `state.json` 無新 key 時以空 map 載入，行為向後相容。

## Capabilities

### New Capabilities
- `session-profile-binding`: 定義動態裝置 session 的 profile 解析來源與優先序（pin > sticky > detect > fallback）、`profile_pins`/`profile_detected` 的持久化契約、`profile_source` provenance 欄位、device_key 穩定性規範，以及 `session.pin`/`session.unpin` 的對外行為與錯誤碼。

### Modified Capabilities
<!-- 無：現有 capability 的 requirement 不變；本案行為屬全新 capability。 -->

## Impact

- `sw_core/session_manager.py`：兩 map load/save（`__init__` 順序）、`_attach_by_id_dynamic` 優先序重構、READY transition 寫 sticky（含 real_path 一致性檢查）、`_template_by_name()`、`SessionRuntime.profile_source`（`__init__` 標 yaml-target）入 `to_public_dict`。
- `sw_core/service.py`：`session.pin` / `session.unpin` RPC 平面分支。
- `sw_core/cli.py`：`pin` / `unpin` subparser（selector 接受 by-path）與 dispatch。
- `state.json` schema：新增 `profile_pins` / `profile_detected`（向後相容）。
- `README.md` / `docs/**`、`CHANGELOG.md`（`[Unreleased]`）、`tests/`。
- 設計來源：`docs/superpowers/specs/2026-06-25-profile-pin-sticky-design.md`（v2，含 codex 對抗式審查修訂）。對應 issue #95。
