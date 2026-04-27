# Design: UART Sense Event → Agent/Tool Trigger

**Issue**: #37
**Status**: Draft v1（brainstorming 完成，pending implementation plan）
**Author**: Paul Chen
**Date**: 2026-04-27

---

## 1. 背景與動機

長時間無人值守的 UART log sensing 場景下，DUT 端會出現各式關鍵事件（kernel panic、watchdog reboot、temperature overhold、特定 prompt 等），目前需要人工 / 外部 script 主動 tail UART 才能反應。需要一個機制讓 agent / tool 能向 serialwrap **註冊「pattern → handler」rule**，命中後由 serialwrap 自動把事件分派出去，由註冊方自行決定後續行為（送 Telegram、拉 baseline、attach STA、…）。

## 2. 核心定位（不可妥協的原則）

1. **udev / crontab 模型**：rule 是宣告式註冊；serialwrap 負責 match + dispatch，**不在自己的 process 裡跑 handler 邏輯**。handler 失敗、卡死、爆量都不能拖累 bridge / WAL / RPC。
2. **附加功能、本質工作優先**：bridge IO loop / WAL / RPC 的 latency 與穩定性高於本功能任何特性。任何 path 不允許回壓 RX。
3. **daemon 不長腦**：不分析 pattern 含義、不知道 `/var/log/*`、不對事件做語義判讀。要把 `/var/log/*` 內容變成可被 match 的事件，是註冊方在 device 上跑 `logread -f` / `dmesg -w` / `tail -f /var/log/...` 的責任。
4. **dispatch 唯一原語 = spawn**：tool 與 agent 都透過 fork/exec 執行註冊方提供的 command。「agent hook / tmux-bridge / acp / mcp / headless cold call」等 6 種 agent 接入模式，**全部由 rule handler command 自行表達**，serialwrap 不為任何一種寫專屬 code path。
5. **保護現行 runtime agent**：per-COM event_trigger 預設 disabled；rule 自帶 `auto_enable_com_on_load` 控制 daemon load 時是否自動上電。

## 3. Goals / Non-Goals

### Goals (v1)

- 註冊方可透過 RPC / CLI / MCP CRUD rule，rule 持久化於 disk。
- 對 device → host 方向的 RX line 做 per-line pattern match（plain substring 或 regex）。
- 可選 scope filter：spontaneous / command_output / any。
- 可選 profile filter：rule 只在 binding 該 profile 的 COM 上生效。
- handler spawn 完整契約：stdin JSON payload + env 子集 + timeout + 並行上限 + overflow drop_oldest。
- counter（fire_count / last_fire_ts / exhausted）persist 在 tmpfs，daemon failover 後沿用，host reboot 清除。
- 事件 / 失敗 / drop 全部寫入 `events.ndjson` 供 forensic。
- daemon 重啟時根據 rule 的 `auto_enable_com_on_load` 自動恢復 monitoring 能力。

### Non-Goals (v1)

- 不做 webhook / HTTP / WebSocket dispatcher（v2 候選）。
- 不做 inbox / pull 模式（永久 out of scope，會違反原則 2）。
- 不做 rule 鏈接（trigger-by-another-event，v2 候選）。
- 不做 daemon 端 level / threshold gating（handler 自己分流）。
- 不做跨 host reboot 的 counter 持久化（host reboot 等於重新 sensing）。
- 不做 RPC caller authentication；`owner` 為 self-declared metadata，daemon 不驗。

## 4. 架構

```
UART device
    │  RX bytes
    ▼
Bridge._handle_serial_rx ──► WAL.append(RX)               (現有，零改動)
                       └──► on_rx_data callback ──► MatcherFeed         (新增)
                                                       │
                                                       ▼
                                           per-COM bounded queue
                                                       │
                                                       ▼
                                       Matcher worker thread (per-daemon, single)
                                              ├ line buffer (\n 切行 + ANSI strip)
                                              ├ scope filter
                                              ├ profile filter
                                              ├ rule pattern match (contains / regex)
                                              ├ cooldown / max_fires gate
                                              └ enqueue Dispatcher
                                                       │
                                                       ▼
                                       Dispatcher pool (max 8 concurrent, per-rule cap = 1)
                                              ├ fork/exec handler
                                              ├ stdin = JSON payload
                                              ├ env  = clean + SERIALWRAP_EVENT_*
                                              ├ timeout(default 10s) → SIGTERM → +1s SIGKILL
                                              └ stdout/stderr → events.ndjson
```

**隔離與背壓策略**：
- per-COM matcher queue 滿（default 1024 lines）→ drop_oldest，記 `event_dropped(reason=matcher_queue_overflow)`。
- per-rule concurrency = 1：rule 還在跑時新事件來 → drop_oldest，記 `event_dropped(reason=per_rule_busy)`。
- per-daemon concurrency = 8：超出 → drop_oldest，記 `event_dropped(reason=per_daemon_saturated)`。
- 任何階段都**不允許 block bridge IO loop** 或回壓 RX。

## 5. Rule Schema

檔案：`events.d/<rule_id>.json`，JSON object（per-rule file，B+Z 模型）。

```json
{
  "schema_version": 1,
  "owner": "tools-static",
  "name": "temp-overhold",
  "rule_id": "tools-static.temp-overhold",

  "kind": "tool",
  "selectors": ["COM0"],
  "profile": "ALL",
  "level": "WARN",

  "pattern": {
    "kind": "contains",
    "value": "Temperature overhold 105C"
  },
  "scope": "spontaneous",

  "max_fires": null,
  "cooldown_ms": 0,
  "timeout_ms": 10000,

  "handler": {
    "exec": ["/usr/local/bin/notice-tool", "--to", "telegram"]
  },

  "auto_enable_com_on_load": true,
  "debug": false
}
```

### 欄位語義

| 欄位 | 型別 | 必填 | 預設 | 說明 |
|---|---|---|---|---|
| `schema_version` | int | yes | `1` | rule schema 版本，daemon load 時驗。 |
| `owner` | string | yes | — | 註冊者識別（self-declared，非 security boundary）。 |
| `name` | string | yes | — | rule 名稱，`[a-z0-9-]+`。 |
| `rule_id` | string | derived | `"<owner>.<name>"` | daemon 寫入；caller 提供時必須等於 derived 值，否則拒絕。 |
| `kind` | enum | yes | — | `"tool"` 或 `"agent"`，metadata only，daemon 不分流。 |
| `selectors` | string[] | yes | — | 適用 COM；可為 `["COM0"]`、`["COM0","COM1"]`、`["ALL"]`。 |
| `profile` | string | no | `"ALL"` | F2 fire-time filter；不寫或 `"ALL"` = 不過濾，否則必須 match COM 當前 session 的 `profile_name`。 |
| `level` | enum | no | `"INFO"` | `INFO/NOTYS/WARN/ERR/ENMR/CRITL`，純 metadata（E1）。 |
| `pattern.kind` | enum | yes | — | `"contains"` 或 `"regex"`（α-X，未來可加 `"glob"`）。 |
| `pattern.value` | string | yes | — | substring 或 Python `re` pattern。 |
| `pattern.flags` | string | no | `""` | regex flag 字串如 `"im"`（i = IGNORECASE, m = MULTILINE 但 v1 line-based 等同無效, s = DOTALL）；contains kind 時 `"i"` 表大小寫不敏感。 |
| `scope` | enum | no | `"spontaneous"` | `"spontaneous"`：matcher fire 時該 COM 沒 active cmd_id 才算；`"command_output"`：有 active cmd_id 才算；`"any"`：兩者皆可。 |
| `max_fires` | int? | no | `null` | `null` = 無上限；`1` = oneshot；`N` = count；達上限 rule 進 `exhausted`、不刪。 |
| `cooldown_ms` | int | no | `0` | 同 rule 兩次 fire 最小間隔；matcher fire 前比對 `now - last_fire_ts`。 |
| `timeout_ms` | int | no | `10000` | handler 執行上限；超時 SIGTERM → +1s SIGKILL。 |
| `handler.exec` | string[] | exec xor shell | — | argv list；不走 shell，無 escaping 風險（β-R）。 |
| `handler.shell` | string | exec xor shell | — | 走 `/bin/sh -c`；escaping 由 caller 自負。**不可與 `exec` 並存**。 |
| `auto_enable_com_on_load` | bool | no | `true` | daemon load rule 時是否自動 enable 該 selectors 上的 event_trigger。 |
| `debug` | bool | no | `false` | true 時 matcher 即使 cooldown / exhausted / scope filter 沒 fire 也寫 `match_skipped`。 |

### Schema 驗證

- `pattern.kind == "regex"` → `pattern.value` 必須能被 `re.compile(value, flags)` 通過，否則 reject。
- `selectors` 元素必須是現有 alias / 已知 COM 名 / `"ALL"`。未來新增的 alias 不會 retroactive，要 reload。
- `handler.exec` 與 `handler.shell` 互斥，且必須二擇一。
- `cooldown_ms`, `timeout_ms`, `max_fires` 須非負（`max_fires` 也可為 null）。

### γ-I 決定

**不**提供 rule 層 `enabled: bool` 欄位。要停用 rule：(a) 刪除 rule，或 (b) 對 selector 做 `event disable`。理由：保持 schema 最小、語義不重疊；declarative deploy 想「定義但不啟用」可以走 `auto_enable_com_on_load: false` + 不主動 `event enable`。

## 6. Lifecycle / Persistence

### 物件位置

| 物件 | 路徑 | 持久層級 |
|---|---|---|
| rule definition | `events.d/<rule_id>.json` | 跨 daemon restart, 跨 host reboot |
| counter（fire_count / last_fire_ts / exhausted） | `/tmp/serialwrap/events/<rule_id>.counter.json` | tmpfs（跨 daemon failover 保留，host reboot 清） |
| event log | `/tmp/serialwrap/events/events.ndjson` | tmpfs（rotation 規則同 daemon log，預設 10MB×3） |
| COM enable 狀態 | runtime in-memory | 無獨立 state file；由 `auto_enable_com_on_load` 在 daemon load 計算 |

`events.d/` 預設位置：`~/.serialwrap/events.d/`（user-writable、跨 reboot 持久），可由 env `SERIALWRAP_EVENTS_DIR` 覆寫；正式部署可指向 `/etc/serialwrap/events.d/` 或 `/var/lib/serialwrap/events.d/`。

### Counter 寫入

- 每次 fire → 更新 `fire_count`、`last_fire_ts`；達 `max_fires` 設 `exhausted: true`。
- 寫法：write tmp file → `os.rename` 原子覆蓋；確保 daemon crash 不留半寫狀態。
- handler 失敗（exit != 0、timeout、spawn 錯）**仍然計入** fire_count，避免重試風暴。

### Counter 清除（對應前述 D 結論）

| 行為 | 是否清 counter |
|---|---|
| `event disable --selector COM0`（COM 上 disable）| 清；對所有「target 包含 COM0」的 rule（B 路線：簡單） |
| rule deletion（檔案被刪 / RPC delete）| 清 |
| `event reset --rule-id X` | 清 |
| 達 `max_fires` 進 exhausted | **不**清（保留證據） |
| daemon restart | **不**清（counter 跨 failover） |

對應原則：disable / delete / reset 是「使用者明確意圖重來」；exhausted / restart 不是。

### 自動 enable（daemon startup）

```
on daemon start:
  load all rules from events.d/
  validate each (failed → log + skip + emit `rule_load_failed`)
  for each loaded rule with auto_enable_com_on_load == true:
      for each selector in rule.selectors:
          mark COM event_trigger enabled
  emit `rule_loaded` per rule
  emit `com_enabled` per COM that got auto-enabled
```

manual `event enable` / `event disable` 之後**不**寫回 rule 檔；下次 daemon restart 仍以 rule 檔的 `auto_enable_com_on_load` 為準。doc + MCP description **強制要求 agent 設定前先呼叫 `event status`**。

## 7. Matcher 行為

### Pipeline

1. Bridge `on_rx_data(data)` callback：把 bytes append 到該 COM 的 line buffer。
2. line buffer 切到 `\n` → 產生 candidate line（保留原始 bytes 的 decoded text）。
3. ANSI / control char strip（用既有 `to_printable` 或新 helper），成 `clean_line`。
4. 若該 COM event_trigger == disabled → 整批 candidate 直接丟。
5. 若該 COM event_trigger == enabled → push 到 per-COM bounded queue（max 1024）。
6. Matcher worker pop line：
   - 對該 COM 的所有 rule 依序評估：
     - profile filter（F2）
     - scope filter（query bridge：該 COM 此刻有沒有 active cmd_id）
     - pattern match（contains / regex on `clean_line`）
     - cooldown gate（`now - last_fire_ts < cooldown_ms` → skip + `match_skipped(cooldown)`）
     - exhausted gate（`max_fires` 已達 → skip + `match_skipped(exhausted)`）
   - 通過 → 構造 payload、enqueue Dispatcher、寫 `match_recorded`。

### Selector 展開

- `selectors=["ALL"]` → daemon 解讀為「所有目前已知 COM」；新增 alias 後須 reload 才生效。
- 多 selector rule fire 時，payload 的 `selector` 欄位是**實際命中的那個** COM。

### Line 上限

per-line 長度上限 16 KB；超出視為「split 後比對」（簡化：直接以 16 KB 切段，rule 作者自行接受）。**v1 不支援跨行 pattern**（已在腦力激盪明確排除 rolling window）。

## 8. Dispatcher / Handler 契約

### Spawn 行為

- `cwd` = daemon cwd（不繼承 caller 的 cwd）。
- 環境：clean env，僅保留：
  - `PATH`（從 daemon 繼承）
  - `HOME`、`USER`、`LANG`、`LC_ALL`（從 daemon 繼承，便於 utf-8）
  - 所有 `SERIALWRAP_EVENT_*`
- handler 不繼承 daemon 的 stdin / stdout / stderr。
- stdin：daemon 寫入 JSON payload 後 close（handler 一次 read 完即可）。
- stdout / stderr：daemon 收走，rotate buffer 取最後 ~4KB 寫入 `fire_completed`。

### Payload（stdin JSON）

```json
{
  "schema_version": 1,
  "rule_id": "tools-static.temp-overhold",
  "rule_name": "temp-overhold",
  "owner": "tools-static",
  "kind": "tool",
  "selector": "COM0",
  "matched_at": 1714200000123,
  "matched_line": "[ 1234.567] sensor: Temperature overhold 105C, throttling",
  "matched_text": "Temperature overhold 105C",
  "match_groups": [],
  "scope": "spontaneous",
  "active_cmd_id": null,
  "wal_seq": 12345678,
  "bridge_generation": 7,
  "fire_count": 3,
  "level": "WARN",
  "profile": "ALL"
}
```

### Env subset（SERIALWRAP_EVENT_*）

| 變數 | 對應 payload 欄位 |
|---|---|
| `SERIALWRAP_EVENT_SCHEMA_VERSION` | `schema_version` |
| `SERIALWRAP_EVENT_RULE_ID` | `rule_id` |
| `SERIALWRAP_EVENT_OWNER` | `owner` |
| `SERIALWRAP_EVENT_SELECTOR` | `selector` |
| `SERIALWRAP_EVENT_MATCHED_AT` | `matched_at` |
| `SERIALWRAP_EVENT_MATCHED_TEXT` | `matched_text`（**不**含 newline；超過 4KB 截斷並加 `...truncated`） |
| `SERIALWRAP_EVENT_FIRE_COUNT` | `fire_count` |
| `SERIALWRAP_EVENT_WAL_SEQ` | `wal_seq` |
| `SERIALWRAP_EVENT_LEVEL` | `level` |
| `SERIALWRAP_EVENT_KIND` | `kind` |
| `SERIALWRAP_EVENT_SCOPE` | `scope` |

### Concurrency 上限（重申）

- per-rule = 1（**doc 必寫**：handler 必須輕量；超出 → `event_dropped(per_rule_busy)`）
- per-daemon = 8（→ `event_dropped(per_daemon_saturated)`）
- 兩個都是 drop_oldest 語義；不 block，不延遲，不影響 bridge。

### Handler 失敗處理

| 情況 | 紀錄 | counter |
|---|---|---|
| 正常退出（exit 0） | `fire_completed{exit=0}` | +1 |
| 非零退出 | `fire_completed{exit=N, stderr_tail}` | +1 |
| 超時被 kill | `fire_timeout` | +1 |
| spawn 失敗（找不到 binary、permission） | `fire_failed{reason}` | +1 |

理由：counter 是「**這條 rule 觸發了幾次**」，不是「handler 成功了幾次」。避免 broken handler 導致無限重試。

## 9. CRUD / RPC / CLI / MCP 介面

### RPC methods（daemon-mediated, X 路線）

| method | 功能 |
|---|---|
| `event.rule_set` | upsert rule（idempotent；G1：rule_id derived from owner.name） |
| `event.rule_delete` | 刪除指定 rule_id 的檔案與 counter |
| `event.rule_list` | 列舉 rule（可 filter `selector`、`owner`） |
| `event.rule_get` | 取單一 rule（含當前 counter / exhausted） |
| `event.com_enable` | 啟用某 COM 的 matcher |
| `event.com_disable` | 停用某 COM；清除該 COM 上 rule 的 counter |
| `event.com_status` | 取 COM 的 enable 狀態、active rule 列表、最近 fire 時間 |
| `event.reset` | 清 counter（指定 rule_id 或 selector 範圍）|
| `event.reload` | rescan `events.d/`，diff apply（add / update / remove） |
| `event.tail` | tail `events.ndjson`（filter `rule_id`, `selector`, `since_ts`, `n`） |

`event.reload` 行為：
- 讀目前所有 `events.d/*.json`，與 in-memory rule set diff。
- 新增的 rule → load + `auto_enable_com_on_load` 評估。
- 移除的 rule → unload + counter 刪除。
- 修改的 rule（檔內容變）→ 視為先 unload 再 load；counter 是否保留 = caller 看；保留行為（穩態升級）。
- 對受影響 COM emit `rule_loaded` / `rule_unloaded` / `com_enabled` / `com_disabled`。

### CLI（透過既有 `serialwrap` entrypoint）

```
serialwrap event add --file <rule.json>           # = rule_set
serialwrap event rm <rule_id>                     # = rule_delete
serialwrap event list [--selector COM0] [--owner X]
serialwrap event show <rule_id>
serialwrap event enable --selector COM0
serialwrap event disable --selector COM0
serialwrap event status [--selector COM0] [--rule-id X]
serialwrap event reset (--rule-id X | --selector COM0)
serialwrap event reload
serialwrap event tail [--rule-id X] [--selector COM0] [-n 50] [--since <ts>]
```

### MCP tools

| tool name | RPC method |
|---|---|
| `serialwrap_event_rule_set` | `event.rule_set` |
| `serialwrap_event_rule_delete` | `event.rule_delete` |
| `serialwrap_event_rule_list` | `event.rule_list` |
| `serialwrap_event_rule_get` | `event.rule_get` |
| `serialwrap_event_enable` | `event.com_enable` |
| `serialwrap_event_disable` | `event.com_disable` |
| `serialwrap_event_status` | `event.com_status` |
| `serialwrap_event_reset` | `event.reset` |
| `serialwrap_event_reload` | `event.reload` |
| `serialwrap_event_tail` | `event.tail` |

### MCP description 必須包含的 contract 文字

每個 MCP tool 的 `description` 都要包含：

> **重要：daemon failover 後，rule 的 `auto_enable_com_on_load` 會自動把 COM 重新打開。請在 enable / disable / 設定前先呼叫 `serialwrap_event_status` 確認當下狀態，避免假設 fresh state。**

## 10. Observability — `events.ndjson`

每行 JSON object，格式：

```json
{
  "ts": 1714200000123,
  "type": "fire_completed",
  "rule_id": "tools-static.temp-overhold",
  "selector": "COM0",
  "...": "..."
}
```

### Event types

| type | 何時寫 | 額外欄位 |
|---|---|---|
| `match_recorded` | matcher 通過所有 gate、enqueue dispatcher | `wal_seq`, `matched_text`, `fire_count` |
| `match_skipped` | **rule.debug=true 才寫**：pattern match 但被某 gate 擋下 | `reason` ∈ `cooldown / exhausted / scope_mismatch / profile_mismatch / per_rule_busy / per_daemon_saturated` |
| `fire_attempted` | dispatcher 即將 fork | `pid`（先不知 → 簡寫 `-1` 後續補也可，v1 簡化為合併進 `fire_completed`） |
| `fire_completed` | handler 正常 / 異常退出 | `exit_code`, `duration_ms`, `stdout_tail`, `stderr_tail`（各 ≤4KB） |
| `fire_timeout` | handler 超時被 kill | `duration_ms` |
| `fire_failed` | spawn 失敗（exec 錯、permission） | `reason` |
| `event_dropped` | drop_oldest 觸發（**永遠寫，不需 debug**） | `reason` ∈ `matcher_queue_overflow / per_rule_busy / per_daemon_saturated`, `dropped_count` |
| `rule_loaded` / `rule_unloaded` | `event.reload` / `rule_set` / `rule_delete` 後 | `rule_id` |
| `rule_load_failed` | schema 驗證失敗 | `path`, `reason` |
| `com_enabled` / `com_disabled` | enable/disable / auto-enable on load | `selector`, `triggered_by` ∈ `manual / auto / reload` |
| `counter_reset` | `event.reset` / disable / delete | `rule_id`, `previous_fires`, `triggered_by` |

### Rotation

`events.ndjson` rotate at 10MB × 3 files（與 daemon log 對齊；具體實作沿用既有 logging handler）。

## 11. Failure Modes / Edge Cases

| 情境 | 行為 |
|---|---|
| `events.d/<id>.json` schema 違規 | daemon load 時 `rule_load_failed`，跳過該檔，不影響其他 rule |
| `pattern.kind=regex` 但 value compile 失敗 | 同上，schema validation 階段擋掉 |
| handler binary 不存在 | 第一次 fire 寫 `fire_failed{reason="exec_not_found"}`，counter +1，下次仍重試 |
| handler 一直 timeout | 每次 timeout +1，直到 max_fires 把 rule 變 exhausted（如未設 max_fires，會持續 fire） |
| matcher queue 滿 | drop_oldest，記 `event_dropped(matcher_queue_overflow)`，bridge 不受影響 |
| daemon crash 在 counter 寫入中 | atomic rename 保證要嘛舊值要嘛新值，不會半寫 |
| host reboot | tmpfs 全清；rule 檔保留；下次 daemon 啟動依 `auto_enable_com_on_load` 重啟 monitor，counter 從 0 開始 |
| COM 未 bind / session 未 attached | 該 COM 上的 rule 不 fire（沒有 RX 流入）；不影響 rule 列表 |
| `selectors=["ALL"]` rule | 套用到所有當前 COM；新增 COM 後須 `event reload` 才會被涵蓋 |
| `profile` filter 但 COM 未 bind | 視為 `actual=null`，不 match；fire skip（rule.debug=true 才寫 `match_skipped(profile_mismatch)`）|

## 12. Out of Scope（v2+ 候選）

- **Webhook / HTTP dispatcher**：以 `handler.kind: "webhook"` 形式擴充，不影響現有 spawn channel。
- **Rule 鏈接（trigger-by-another-event）**：需要 daemon 內部 event bus，v1 故意不做。
- **Daemon 端 level / threshold gating**：使用者抱怨 handler-side filter 不夠用時再加。
- **Cross-host-reboot counter persistence**：搬到 `/var/lib/serialwrap/events/` + atomic write；目前用例不需要。
- **RPC caller authentication**：與整個 serialwrap 的 caller auth 一起處理，本 issue 不涵蓋。
- **Rolling-window pattern（跨行 match）**：v1 故意只做 line-based。
- **Glob pattern type**：未來在 `pattern.kind` 加 `"glob"` 即可，不需動 outer schema（α-X 設計目的）。

## 13. 對既有 code 的改動範圍

| 檔案 | 改動 |
|---|---|
| `sw_core/uart_io.py` | `Bridge` 多接一個 `MatcherFeed` 訂閱 `on_rx_data`（既有 hook，零回壓設計確認） |
| `sw_core/session_manager.py` | service 啟動 / 停止 matcher worker；提供「該 COM 此刻是否有 active cmd_id」query API 給 scope filter |
| `sw_core/service.py` | 註冊新 RPC method、在 daemon startup 載入 rules + 評估 auto_enable |
| `sw_core/cli.py` | 新增 `event` 子命令群 |
| `sw_mcp/server.py` | 新增 10 個 `serialwrap_event_*` tool definition |
| `sw_core/event_engine.py`（新檔）| RuleRegistry / Matcher / Dispatcher / EventLog；本 feature 主要邏輯 |
| `sw_core/constants.py` | `EVENTS_DIR`（rule 持久層）、`EVENTS_RUNTIME_DIR`（counter / event log）env 與預設 |
| `docs/design-event-trigger.md` | 本文件 |
| `tests/event_engine/` | 單元測試（matcher、scope filter、cooldown、max_fires、counter persistence、reload diff） |
| `func-test/event_engine/` | end-to-end 行為測試（fake bridge → matcher → spawn echo handler） |

## 14. 開發階段切分（leader → 後續 implementation plan 用）

> 本節僅作為 planning reference，detailed plan 由 writing-plans skill 產出。

- **Phase 1**：RuleRegistry（檔案 IO + schema 驗證 + reload diff）+ CLI/MCP CRUD，無 matcher。可以驗收 declarative deploy / list / show。
- **Phase 2**：Matcher worker（per-line + scope + profile + cooldown + max_fires），但 dispatcher 用 stub（只寫 event log，不 spawn）。可驗收 match 邏輯。
- **Phase 3**：Dispatcher（spawn pool、payload、env、timeout、overflow、failure 紀錄）。
- **Phase 4**：Counter persistence + auto_enable_com_on_load + daemon failover behavior。
- **Phase 5**：Observability 收尾（`event tail`、`debug=true` skip 紀錄、rotation）。
- **Phase 6**：Doc、README、MCP description「先呼叫 status」字串、func-test、release note。

---

## Appendix A：典型 rule 範例

### A1. 溫度告警送 Telegram（issue 原文範例）

```json
{
  "schema_version": 1,
  "owner": "tools-static",
  "name": "temp-overhold",
  "rule_id": "tools-static.temp-overhold",
  "kind": "tool",
  "selectors": ["COM0"],
  "level": "CRITL",
  "pattern": { "kind": "contains", "value": "Temperature overhold 105C" },
  "scope": "spontaneous",
  "max_fires": null,
  "cooldown_ms": 60000,
  "timeout_ms": 5000,
  "handler": {
    "exec": ["/usr/local/bin/notice-tool", "--to", "telegram", "--channel", "ops"]
  },
  "auto_enable_com_on_load": true
}
```

### A2. Kernel panic 一次性抓現場

```json
{
  "schema_version": 1,
  "owner": "agent-claude-soak",
  "name": "kernel-panic-snapshot",
  "rule_id": "agent-claude-soak.kernel-panic-snapshot",
  "kind": "agent",
  "selectors": ["ALL"],
  "level": "CRITL",
  "pattern": { "kind": "regex", "value": "Kernel panic - not syncing: ", "flags": "" },
  "scope": "spontaneous",
  "max_fires": 1,
  "cooldown_ms": 0,
  "timeout_ms": 30000,
  "handler": {
    "exec": ["/opt/agents/claude/relay-kernel-panic.sh"]
  },
  "auto_enable_com_on_load": true
}
```

handler 內可呼叫 `serialwrap_wal_range --since-seq $SERIALWRAP_EVENT_WAL_SEQ-200 --until-seq $SERIALWRAP_EVENT_WAL_SEQ+50` 抓上下文。

### A3. 在 dmesg 串流中找特定 keyword（scope=command_output）

前提：agent 已在 device 上跑 `dmesg -w &`。

```json
{
  "schema_version": 1,
  "owner": "tools-static",
  "name": "dhd-firmware-trap",
  "rule_id": "tools-static.dhd-firmware-trap",
  "kind": "tool",
  "selectors": ["COM0"],
  "profile": "brcm",
  "level": "ERR",
  "pattern": { "kind": "regex", "value": "dhd_dpc.*firmware_trap" },
  "scope": "any",
  "max_fires": null,
  "cooldown_ms": 30000,
  "timeout_ms": 5000,
  "handler": {
    "shell": "echo \"$SERIALWRAP_EVENT_MATCHED_TEXT\" | logger -t serialwrap-event"
  },
  "auto_enable_com_on_load": true
}
```
