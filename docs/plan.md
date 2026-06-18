> 📌 **歷史快照**（截至 Phase 3）。本檔不反映 #51 / #53 / #54 / #55 之後的現況，僅留作歷史，不再維護。最新狀態見 `README.md` 與 `openspec/specs/*`。

# serialwrap — 專案計畫

## 願景

讓多個 agent 與 human console **安全共用同一條 UART**，提供 broker 架構的 daemon + RPC + MCP 介面。

## 現況快照

| 面向 | 狀態 |
|------|------|
| Daemon + RPC | ✅ 穩定運作 |
| CLI subcommand | ✅ 完整 |
| MCP adapter | ✅ `_TOOL_MAP` 同步 |
| WAL 雙軌記錄 | ✅ raw.wal.ndjson + raw.mirror.log |
| Agent log capture | ✅ `session.log_start` / `session.log_stop` |
| Human console (raw interactive) | ✅ suspend/resume 機制 |
| Profile / template 靜態綁定 | ✅ targets → COM |
| **Auto-detect template** | ✅ 動態偵測 + 動態 session |
| 單元測試 | ✅ 172 tests 全過 |

## Phase 記錄

### Phase 1 — 核心 broker 架構（已完成）

- daemon + RPC + CLI + MCP
- session 狀態機（DETACHED → ATTACHING → ATTACHED → READY）
- CommandArbiter 單寫入者排隊
- WAL 雙軌記錄
- human console raw interactive + suspend/resume

### Phase 2 — 多裝置支援（已完成）

- profiles YAML：template + targets
- DeviceWatcher hotplug 偵測
- per-session 帳密隔離（auth.py）
- alias / binding 持久化（state.json）

### Phase 3 — Auto-detect template + 動態 session（已完成）

- `detect_template()` 偵測 UART 輸出匹配最佳 template
- 動態 session 建立（不需在 targets 預定義 COM slot）
- `max_sessions` 上限保護（預設 16）
- targets 區段變為可選
- prpl-template 恢復為可偵測模板

## 設計原則

1. **Daemon 是唯一寫入者**：只有 daemon 直接碰 UART
2. **Config 不可變，Runtime 可變**：frozen dataclass vs mutable dataclass
3. **RPC 路由平面化**：flat if/elif，不做動態註冊
4. **顯式同步多個表面**：新增能力要同步 service / CLI / MCP / docs / tests
5. **JSON 輸出緊湊穩定**：`ensure_ascii=False, separators=(",", ":")`
