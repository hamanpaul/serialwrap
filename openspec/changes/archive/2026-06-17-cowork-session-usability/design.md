## Context

完整設計見 `docs/superpowers/specs/2026-06-17-cowork-session-usability-design.md`，本文摘錄關鍵技術決策。

現狀：serialwrap 的 console 共用本意是**禮讓**（`uart_io.py` 的 `_interactive_owner`/`_agent_active`/`_suspended_owner`/`_deferred_buffers`，human 鍵入在 agent 注入期間進 deferral 暫存後回放）。但 human lease 一旦持有就無 idle/staleness 判定（`_lease_context` 的 `human_attached` 只是「有無 human lease」），孤兒 minicom 使其退化成無限期硬鎖（#53）。同時 passthrough 在 `_attach_by_id` 寫死 `ok=False` 永停 `ATTACHED`，`cmd submit` 回語意不清的 `SESSION_NOT_READY`（#51）。

## Goals / Non-Goals

**Goals:**
- 閒置/孤兒 human lease 不再無限期擋人，且保留「不洗掉 human 畫面」原意。
- passthrough/無 prompt profile 明確表達不可下命令；有 `ready_probe` 的 target 能進 READY。
- READY 泛化至 bootloader（U-Boot），並在真機 COM1 驗證。

**Non-Goals:**
- #52 bulk transfer fairness/QoS。
- long-idle 自動清理 console（交 agent 主動 `recover`/`console-detach`）。
- 改變 `human_attached` 既有語意（採 additive 新欄位）。
- bootloader 互動/燒錄路徑（#44 recovery lease 維持原樣，僅確認與新 line-command 路徑並存）。

## Decisions

- **判準採「真實鍵入時間窗 + liveness」**（而非純 idle timeout 或純 process 偵測）：`idle_for_ms` 被 broker probe 洗小不可靠；孤兒 minicom 多半 alive，故 alive-but-idle 用時間窗（`HUMAN_ACTIVE_WINDOW_S=60s`）、真正關閉(peer gone)用既有 liveness。
- **soft preempt 而非 hard evict**：idle 真人只降級（沿用 suspend/deferral，輸入畫面不丟）；只有 liveness 判定已死才 detach。最貼近「lease 是禮讓非硬鎖」原意，誤搶成本低。
- **additive `human_active`，不改 `human_attached`**：降低破壞既有 consumer/測試風險（`session-selftest` 既有契約大量依賴 `human_attached`）。擋人邏輯改看 `human_active`。
- **command_capable = `ready_probe` 非空**（而非新增 profile 欄位）：零 schema 擴張、與既有 `ready_probe` 機制自洽；passthrough 設了 `ready_probe` 即可用。
- **error code `PROFILE_NOT_COMMAND_CAPABLE`**：與「command-capable 但尚未 READY」的 `SESSION_NOT_READY` 區隔，語意清楚。
- **READY 泛化**：READY = 「對得上預期 prompt + `ready_probe` round-trip」，OS/bootloader 一致。U-Boot 綁對 profile（prompt_regex 對提示符、`ready_probe` 用 `echo`）即可進 READY。

## Risks / Trade-offs

- soft preempt 動到 co-work 核心（suspend/resume + deferral）→ **重點測位元組不交錯、回放正確，及「agent 結束與 human 重新活躍同時」競態**。Mitigation：fake PTY 整合測試覆蓋這些路徑。
- `command_capable` 只 gate 進 READY；退化 `prompt_regex`（如 others-template 的 `.*`）下 line 輸出框取不可靠 → **使用者 profile 責任**，文件註明。
- 對外行為變更（`PROFILE_NOT_COMMAND_CAPABLE`、新欄位）可能影響下游 → Mitigation：additive 欄位 + 在 proposal/README 明列變更。
- U-Boot 真機驗證需板子能進 bootloader（可能要 reset/中斷開機）→ Mitigation：驗證前先確認 COM1 實機狀態與 prompt 字串。

## Migration Plan

- 純新增/語意精化，無資料遷移。部署即 daemon 重啟生效。
- 回退：revert 該 change 即恢復舊行為（passthrough 仍 ATTACHED、human lease 仍硬鎖）。

## Open Questions

- U-Boot `prompt_regex` 確切字串與 `platform` 是否需新增列舉值 → 真機驗證 + plan 階段定。
