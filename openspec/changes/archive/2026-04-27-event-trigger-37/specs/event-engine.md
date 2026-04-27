# Spec: Event Trigger Engine

> Capability: event-engine
> Status: Implemented (Issue #37, branch feat/event-trigger-37)

## Rule 格式

```json
{
  "owner": "myagent",
  "name": "reboot-detect",
  "pattern": {"kind": "contains", "value": "reboot completed"},
  "handler": {"shell": "touch /tmp/reboot-fired"},
  "selector": "COM0",
  "scope": "spontaneous",
  "max_fires": 0,
  "cooldown_ms": 1000,
  "timeout_ms": 10000,
  "auto_enable_com_on_load": true,
  "debug": false
}
```

### 欄位規格

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| owner | str | 必填 | 英數+底線 |
| name | str | 必填 | 英數+底線+連字號 |
| pattern.kind | enum | 必填 | contains / regex / starts_with / ends_with |
| pattern.value | str | 必填 | 搜尋字串或正則表達式 |
| pattern.case_insensitive | bool | false | 忽略大小寫 |
| handler | dict | 必填 | `{"exec": [...]}` 或 `{"shell": "..."}` 二擇一 |
| selector | str\|null | null | COM filter，null 代表全部 |
| scope | enum | spontaneous | spontaneous / command_output / any |
| max_fires | int | 0 | 0 = 無限 |
| cooldown_ms | int | 0 | 0 = 不冷卻 |
| timeout_ms | int | 10000 | handler 逾時（ms） |
| auto_enable_com_on_load | bool | true | 載入時自動啟用對應 COM |
| profile | str\|null | null | profile filter |
| debug | bool | false | 啟用 emit_skipped 日誌 |

## RPC / CLI / MCP 對照表

| RPC method | CLI | MCP tool |
|------------|-----|----------|
| event.add | serialwrap event add | serialwrap_event_add |
| event.delete | serialwrap event delete | serialwrap_event_delete |
| event.get | serialwrap event get | serialwrap_event_get |
| event.list | serialwrap event list | serialwrap_event_list |
| event.enable_com | serialwrap event enable-com | serialwrap_event_enable |
| event.disable_com | serialwrap event disable-com | serialwrap_event_disable |
| event.reload | serialwrap event reload | serialwrap_event_reload |
| event.reset | serialwrap event reset | serialwrap_event_reset |
| event.status | serialwrap event status | serialwrap_event_status |
| event.tail | serialwrap event tail | serialwrap_event_tail |

## 安全契約

⚠️ 呼叫 enable / disable 前必須先呼叫 status。

## 儲存路徑

| 資料 | 路徑 |
|------|------|
| 規則目錄 | `~/.serialwrap/events.d/` |
| 計數器（tmpfs） | `/tmp/serialwrap/counters/` |
| 事件日誌 | `/tmp/serialwrap/wal/events.ndjson` |
| 備份 | `/tmp/serialwrap/wal/events.ndjson.1` … `.5` |

## Handler 守則

- 在 timeout_ms 內結束（超時：SIGTERM pgid → 1s wait → SIGKILL pgid）
- 不可呼叫 setsid() / daemonize（脫離 process group 導致 timeout 失效）
- 從 stdin 讀取 JSON payload
- exit 0 = 成功，非 0 = 失敗（均記入 events.ndjson）
- 保持冪等性

## 事件日誌欄位

```json
{
  "type": "fire_completed",
  "rule_id": "myagent.reboot-detect",
  "com": "COM0",
  "matched_text": "reboot completed",
  "trigger_ts": 1700000000.0,
  "exit_code": 0,
  "stdout_tail": "",
  "stderr_tail": ""
}
```

type 可為：fire_submitted / fire_completed / fire_failed / fire_timeout / event_dropped / emit_skipped
