## Context

動態偵測 session 的 profile 由 daemon 啟動時 `detect_template()` probe 決定，從不持久化（`state.json` 只存 `aliases`/`bindings`/`released`），故每次重啟重新偵測、時序敏感會漂移。現有 `bind`/`attach`/`clear` 皆改不了既有 session 的 profile。完整逐項設計與程式碼佐證見 `docs/superpowers/specs/2026-06-25-profile-pin-sticky-design.md`（v2，含 codex 對抗式審查修訂）。本文件聚焦決策與理由。

## Goals / Non-Goals

**Goals:**
- core 通用機制持久化動態裝置 profile，根治重啟/重裝後漂移。
- 混合機制：手動 `pin`（權威）+ 自動 `sticky`（記住達 READY 的偵測），漂移自我收斂。
- 向後相容（舊 `state.json` 無新 key → 空 map）。

**Non-Goals:**
- 不改善偵測本身穩定度（屬 #69 自動重探範疇）。
- sticky/pin 命中後 attach/ready 失敗**不自動推翻**。
- 不改 YAML `targets` 既有語意；只服務無 explicit target 的動態裝置。

## Decisions

- **混合機制（pin > sticky > detect > fallback 四層優先序）**；替代：純 explicit pin（每裝置要手動，無自動）、純自動 sticky（無權威逃生艙）。選混合兼得確定性與零日常操作。
- **sticky 改「達 READY 才寫」**（非「detect 成功就寫」）；替代被否決：detect 成功立即寫會繼承 `_attach_by_id_dynamic` 的 TOCTOU（probe real_path 與 final bridge real_path 可能不同，把 A 裝置結果記到 B），且 broad regex 被 boot log 誤中的 false positive 無法自我收斂。達 READY=`ready_probe` nonce 已驗證=確定正確，一招解兩問題。
- **device_key 採穩定鍵、同晶片 by-id 碰撞用 by-path**；替代：純 by-id 字面會在同款 CH340（by-id 相同）張冠李戴（CLAUDE.md/README 明載須 by-path）。
- **新增 `profile_source` provenance 欄位，一物三用**（顯示 / sticky 寫入判斷 / explicit-target 判斷）；替代被否決：用「by-id 是否在某 map」反推 explicit 不可靠（codebase 原無 provenance，YAML session 與動態 session 的 SessionRuntime 無型別區分）。
- **pin/sticky 命中跳過 probe**：省一次 probe 並避開吐 log 干擾；只有走到 detect 順位才開 PROBE bridge。
- **平面 RPC 分支 `session.pin`/`session.unpin`**：沿用既有平面 if/elif 分派慣例，不引入 registry。

## Risks / Trade-offs

- [同晶片 by-id 碰撞 pin/sticky 錯置] → device_key 用 by-path；文件明示多板同晶片用 by-path 綁定。
- [`uboot-template` 等 passthrough 正向偵測不達 READY] → 不寫 sticky、每次重啟重 probe（其 prompt 明確、偵測穩定、成本低）。
- [pin 的 profile UART 參數與實際不符 → 亂碼] → 依「不自動推翻」不介入；屬 pin 錯的使用者責任。
- [sticky/detected attach 後達不到 READY] → 停在該 profile 狀態，靠 #69 reprobe / 人工 `unpin`/`pin` 修正。
- [`__init__` 啟動即 `_save_state` 洗掉新 key] → 先初始化兩 dict 再 `_load_state`、`_save_state` payload 同步納入。
- [寫 sticky 與 FLASHING/RELEASED 競態] → 寫盤一律 `_lock` 內並守 FLASHING/RELEASED 不被覆寫。

## Migration Plan

- 純新增、無 BREAKING：舊 `state.json` 無新 key 以 `.get(..., {})` 空 map 載入。
- 部署即生效；rollback 為還原程式碼即可，遺留的 `profile_pins`/`profile_detected` 對舊版為未知 key、被忽略，無副作用。

## Open Questions

- 是否需要 `session redetect`（清單一裝置 sticky、強制重偵測）？目前 YAGNI——`pin` 正確值即可覆蓋 sticky；列為 issue #95 後續，本案不做。
