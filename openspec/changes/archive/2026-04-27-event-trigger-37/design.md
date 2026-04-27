# Design: UART RX Event Trigger Engine

## 架構概覽

```
UARTBridge._on_bridge_rx
    └── SessionManager._rx_observers[](com, data, seq)
            └── SerialwrapService._engine_rx_observer()
                    └── EventEngine.feed_line(com, line)
                            └── MatcherWorker (per-COM thread, bounded queue)
                                    └── Dispatcher (thread pool, subprocess spawn)
```

## 關鍵設計決策

### 1. Zero back-pressure 原則

bridge RX 通知必須在 microseconds 內返回。所有下游處理走 bounded queue + drop_oldest；
任何 IO / subprocess 操作在獨立 thread 執行。

### 2. `sw_core/event_engine/` 套件結構

| Module | 責任 |
|--------|------|
| `schema.py` | Rule frozen dataclass + validate_rule_dict() |
| `counter.py` | CounterStore atomic save/load/clear（tmpfs） |
| `registry.py` | RuleRegistry write-through cache + disk diff |
| `event_log.py` | NDJSON append-only logger，rotation + tail |
| `line_buffer.py` | per-COM byte→line splitter + ANSI strip |
| `matcher.py` | PatternMatcher gates + MatcherWorker thread |
| `dispatcher.py` | subprocess pool，timeout + pgid kill |
| `engine.py` | EventEngine orchestration，lifecycle，COM toggle |

### 3. Thread Safety

- `EventEngine`：`threading.RLock`（rule CRUD + COM toggle 需要 reentrant）
- `MatcherWorker`：`threading.Lock`（規則替換時短暫持鎖）
- `_engine_line_buffers` dict（service.py）：`threading.Lock`（dict 操作非原子）
- `EventLogger`：per-write lock + fsync（forensic 保障）
- `Dispatcher`：`threading.Lock`（in-flight tracking）

### 4. RX Observer 契約

observer callback **不可**：
- 阻塞在任何 IO 或跨 thread 等待
- 在持 `SessionManager._lock` 時呼叫 SessionManager 方法（deadlock 風險）

### 5. COM 啟用策略

- 預設：所有 COM 停用（matcher 不處理 incoming lines）
- `auto_enable_com_on_load: true`（per-rule）：daemon 啟動載入規則後自動啟用對應 COM
- Agent 仍可隨時呼叫 `event.enable_com` / `event.disable_com`

### 6. Observer 注冊時機

`add_rx_observer()` 在 `service.start()` 中呼叫（engine.start() 之後），
而非 `__init__`，確保 engine worker threads 已就緒再接收 RX 事件。

### 7. 欄位適配

計劃原稿提到 `_sessions_by_com`（不存在）；實作改為掃描 `self._sessions.values()`。
計劃原稿提到 `session.fg_cmd_id`（不存在）；返回 sentinel string `"foreground"`。

## API 表面

10 RPC 路由 + 10 CLI 子命令 + 10 MCP 工具，對稱覆蓋：
add, delete, get, list, enable_com, disable_com, reload, reset, status, tail

## 安全契約

MCP descriptions 與 README 均明確要求：呼叫 enable/disable 前必須先呼叫 status。
