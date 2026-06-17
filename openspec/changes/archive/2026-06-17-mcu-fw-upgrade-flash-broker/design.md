## Context

完整設計見 `docs/superpowers/specs/2026-06-17-mcu-fw-upgrade-flash-broker-design.md`（brainstorming 產出）。
本文件摘錄技術決策與理由，供實作前定錨。

現況（`sw_core/uart_io.py`）：底層 termios 已是 raw（`_configure_serial:118`），device→console 也是
raw 1:1（`_handle_serial_rx:329`），但三道牆使 `ocp-mcu-upgrade` 無法走通：
(1) `_loop:431/468` daemon 永遠在讀 real device → 與 flasher 形成 two-reader race；
(2) `_handle_console_rx:398` 只有 human owner 走 `send_bytes` 原樣送，其餘走 `_consume_console_input:367`
行處理 → binary TX 被汙染；(3) passthrough profile 不改 byte 行為。
另：`/dev/ttyUSBx` 與 by-id 都會漂移（每次重燒轉接板可能不同），唯一不變的是「行為」。

## Goals / Non-Goals

**Goals:**
- daemon 持續 maintain tty、其他 COM 不受影響下，提供 byte-transparent `/dev/ttyMCU`，讓外部 flasher 走通。
- 以非破壞性 sync-probe 自動認 MCU 線，不依賴會漂的 ttyUSBx/by-id。
- 可擴充多家 MCU（pattern registry，預設 TI CC2674/CC2652）。
- 全程保留 RAW WAL 證據；結束自動恢復 console；daemon 全程為 real device 唯一 reader。

**Non-Goals:**
- 把 SBL / `ocp-mcu-upgrade` 完整 flash 協定塞進 daemon（只持最小 sync 握手知識）。
- BSL-invoke（GPIO reset）編排：v1 仍走既有 DUT console cmd。
- 透過 PTY 路由「破壞性 flasher bytes 去 probe」。
- `device release` 式整個釋放裝置（#54 已做，與本設計正交）。

## Decisions

| 決策 | 選擇 | 替代方案 / 為何不選 |
|------|------|------|
| 燒錄通道 | **B 案：serialwrap 出 byte-transparent PTY 端點** | A 案（daemon 暫停 reader、flasher 開 real device）：可行但 binary 不經 serialwrap，無 RAW evidence；且使用者明確要「daemon maintain tty」 |
| 認線 | **非破壞性 sync-probe（行為判別）** | 綁 alias/by-id：轉接板會換，綁死下次就錯；by-path：僅多顆同款無序號時才需要 |
| 協定知識 | **最小 + 可擴充 registry** | 整合完整 flasher 協定：耦合重、維護成本高，#55 明確不要 |
| 端點分流 | **依「有沒有送 bytes」**：write→flash、read-only→列表 | 靠猜 client 意圖：脆弱 |
| 偵測不到 MCU | **不回合成錯誤，正常 timeout + 週期 re-probe** | 回 NO_MCU 錯誤：與 flasher 自身 retry 重複、體驗差；re-probe 讓遲到 BSL 仍能 latch |
| 多候選 ACK | **FLASH_AMBIGUOUS 不自動挑** | 自動挑：誤燒風險 |
| 狀態/恢復 | **新增 `FLASHING`，沿用 release/attach 骨架** | 全新獨立路徑：重造輪子、與既有恢復不一致 |
| flash 期間 console | **唯讀快照；RAW WAL 全程保留** | 全 detach：少了過程稽核（使用者要求留 WAL） |

## Risks / Trade-offs

- [double-sync：serialwrap probe 後 flasher 自身 connect/sync 可能受殘響干擾] → 命中後 `clear_rx_buffer`
  清殘響再開 bridge；TI ROM 可重複 sync；**列強制真機 gate 驗證**。
- [PTY 多一跳的 latency/buffering 影響 SBL timing] → real device 與 PTY 皆 raw、master 非阻塞；
  **真機 gate 以實燒 `0x0` 驗證**；必要時調 read chunk / 關閉 PTY 行緩衝。
- [baud 不一致：flasher 對 PTY slave `tcsetattr`，real device 未跟上] → 將 slave termios（baud/framing）
  鏡射到 real device，fallback 用命中 pattern 的 registry baud。
- [registry 被塞入破壞性「probe」序列] → 非破壞不變式 + probe bytes 限「已審核」清單。
- [偵測誤判燒到 console] → 排除 `command_capable` console；明指 console 需 `--force`；多 ACK 不自動挑。
- [flash 未結束/crash 後卡 FLASHING] → 結束條件含 hangup/timeout/顯式 end；恢復用 `_probe_external_holder`
  + `_spawn_attach`，失敗停 `ATTACHING` + 明確 `last_error`（與既有 attach 一致）。

## Migration Plan

- 純新增能力，無破壞性變更：未啟用 flash 端點時行為與現況一致。
- 部署：daemon 啟動時建 `${RUN_DIR}/dev/ttyMCU`（預設）；要 `/dev/ttyMCU` 另附 udev/root symlink 說明。
- 回滾：移除端點 + FLASHING 路徑即回到 #54 的 `device release` 手動 handoff，無資料遷移。

## Open Questions

- `mcu` RPC 的最終形狀：是否需要顯式 `flash begin/end`，或純靠端點 open/close 自動進出（傾向自動 + 可選顯式 end 作 fallback）——實作時定，不影響 spec 行為需求。
- 多 pattern 時 probe 順序/平行度（候選 × pattern）；先序列、命中即止，未來再優化。
