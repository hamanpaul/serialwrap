## Context

完整設計見 `docs/superpowers/specs/2026-06-15-device-release-handoff-design.md`，本文件聚焦技術決策與取捨。

serialwrapd 對任何 attached session 持續開啟並讀取真實 UART（`uart_io.py:174` 開 FD 無 `TIOCEXCL`、`uart_io.py:444` `_loop` 持續 `os.read`）。外部 flasher 對同一 raw device 做 SBL 二進位協定時，daemon 是「持續讀取的第二 reader」，造成 byte 競爭、MCU fw upgrade 間歇失敗（#54）。`session clear`（`session_manager.py:516`）detach 後立即 `_spawn_attach` re-attach（`:529`），等於放不掉；唯一乾淨手段 `daemon stop` 會殺掉所有 port。

關鍵結構發現：所有自動 attach 路徑（`clear_session`、`update_devices:645`、`bootstrap_attach:651`、recovery force re-attach）都匯流到單一函式 `_spawn_attach(by_id)`（`session_manager.py:653`）。

## Goals / Non-Goals

**Goals:**
- 提供 surgical、可恢復、不被自身 re-attach 搶回、跨重啟保留的單一 device handoff。
- daemon 持續運作、其他 COM port 不受影響。
- 避免反向故障（被忘記的 release 不可變成裝置永久不可用）。

**Non-Goals:**
- #55 原生 fw upgrade（serialwrap 內建/整合 flasher）。
- 自動偵測還原（auto-reclaim）、TTL lease。
- 對 serialwrap 自身 attach 加 `TIOCEXCL`。

## Decisions

- **D1 兩步式手動 handoff（release → attach）**，而非自動還原。
  - 理由：flasher 在 erase→program 間會多次 open/close raw device，自動 reclaim 會在燒錄中途插入弄壞 binary。明確兩步競態最少、可預測。
  - 替代方案：自動偵測還原（否決，race）；TTL lease（否決，燒錄時間不定、逾時打斷更糟）。
- **D2 single choke-point guard**：在 `_spawn_attach(by_id)` 最前面檢查 released by-id 集合並 return。
  - 理由：一處 guard 涵蓋全部 re-attach 路徑；把現有 placeholder workaround 正規化。
  - 替代方案：逐 call-site 加 guard（否決，易漏）；device-level exclusion set 為唯一 source of truth（否決，與 session 狀態雙頭）。
- **D3 source of truth 在 session 欄位**，`_released_by_ids` 僅為衍生熱路徑快取，release/attach/load 三處同步。
- **D4 clean-slate console**：release 以 `_detach_session_locked(drop_consoles=True)` 關 FD 並丟棄 console（不 stash）；attach 走一般路徑重建 primary console。
  - 理由：最接近「裝置完整交出」語意；避免 stale minicom（對 #53 孤兒 console 有附帶清理）。
- **D5 持久化 + bootstrap 前還原**：`_save_state`/`_load_state` 擴充 `released` map，`_load_state` 在 `bootstrap_attach` 前還原，避免重啟搶回燒錄中的裝置。
- **D6 唯讀 idle 標註**：`_probe_external_holder` 讀 `/proc`/`lsof`（不開 tty），`self_test` 在 RELEASED 下回 `external_holder`/`reclaimable`/`recommended_action`，並作為 `device.attach` 的安全 guard（`DEVICE_STILL_HELD`，`--force` 可略過）。

## Risks / Trade-offs

- [release 被忘記/crash → 裝置長期不可用] → RELEASED 大聲可見（provenance）+ 唯讀 idle 標註讓「可安全收回」自動浮現；一條 `device.attach` 收回。
- [`_released_by_ids` 與 session 欄位漂移] → session 欄位為 source of truth，set 僅衍生；三處同步並有對抗測試覆蓋。
- [唯讀偵測誤判] → 僅標註、不自動動作，誤判最多讓 `recommended_action` 暫時不準，不影響燒錄。
- [clean slate 強拆人類 minicom 造成困惑] → response 透明回報；屬刻意選擇。
- [持久化 released 在裝置已永久移除後殘留] → `update_devices` 的 `DEVICE_REMOVED` 與 `DEVICE_NOT_PRESENT` 可辨識。

## Migration Plan

- 純新增能力，無 schema 破壞。新狀態 `RELEASED` 與新欄位對既有 client 為附加式（既有欄位不變）。
- 持久化 state 向後相容：舊 state 無 `released` 鍵時視為空集合。
- Rollback：移除 `device.release`/`device.attach` 與 guard 即回到舊行為；持久化的 `released` 鍵被忽略即可。

## Open Questions

- 無阻斷性未決項。`source` 是否改由 RPC caller 身分自動帶入、`--ttl` 是否未來 opt-in，留待 #55 一併考量。
