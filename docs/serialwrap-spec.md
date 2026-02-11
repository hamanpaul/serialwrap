# serialwrap 二次開發重構規格書（v2）

## 1. 文件目的
本文件定義 `serialwrap` 二次重構的決策完整規格，目標是在單一 UART 下支援多 Agent 並行協作，保證資料完整性、可追溯性、可治理性，並支援動態 UART hotplug。

## 2. 核心目標與原則
### 2.1 目標
- 導入常駐 `serialwrapd`。
- broker 獨占實體 UART；Agent 走 RPC；人類 `minicom` 走 broker 提供 PTY。
- 保證 RAW 記錄完整保存（TX/RX/ts/source/cmd_id）。
- 支援動態增減 UART，保留 session、失連不重試、同 by-id 自動掛回。

### 2.2 核心不變量
- 單 UART 單寫入者。
- 全事件有 `seq`。
- 任一結果可回溯 `cmd_id`。
- 發生遺失/截斷不可靜默。

## 3. 決策總覽
- 架構：一次到位 daemon。
- IPC：Unix Socket JSON-RPC。
- WAL：NDJSON + base64。
- RAW log：雙軌輸出（`raw.wal.ndjson` + `raw.mirror.log`）。
- 抗雜訊：RAW/Result/Decision 三層。
- Login：profile 自動 FSM（BCM/prpl）。
- COM 映射：`/dev/serial/by-id`。
- alias：預設 `PROFILE+ActNo`，可重指派。
- hotplug：保留 session，失連不重試，僅同 by-id 自動掛回。
- 多開：daemon singleton lock + socket owner 檢查。

## 4. 範圍
### 4.1 In Scope
- `serialwrapd`、仲裁器、命令池、結果池。
- UART bridge、PTY bridge、WAL/mirror。
- device watcher、session/alias 管理。
- CLI client 與 MCP adapter。

### 4.2 Out of Scope
- target 端 tmux 多 shell。
- sideband 通道。

## 5. 目錄規劃
- `serialwrapd.py`
- `serialwrap`
- `sw_core/arbiter.py`
- `sw_core/uart_io.py`
- `sw_core/login_fsm.py`
- `sw_core/wal.py`
- `sw_core/device_watcher.py`
- `sw_core/session_manager.py`
- `sw_core/alias_registry.py`
- `sw_core/rpc.py`
- `sw_mcp/server.py`
- `profiles/*.yaml`

## 6. Public 介面
### CLI
- `serialwrap daemon start|stop|status`
- `serialwrap device list`
- `serialwrap session list|clear|bind|attach`
- `serialwrap alias list|set|assign|unassign`
- `serialwrap cmd submit|status|cancel`
- `serialwrap stream tail`
- `serialwrap log tail-raw|tail-text`
- `serialwrap wal export`

### RPC
- `health.ping`, `health.status`
- `device.list`
- `session.list`, `session.get_state`, `session.clear`, `session.bind`, `session.attach`
- `alias.list`, `alias.set`, `alias.assign`, `alias.unassign`
- `command.submit`, `command.get`, `command.cancel`
- `result.tail`
- `log.tail_raw`, `log.tail_text`
- `wal.range`

## 7. Profile/Target 規格（解耦）
### 7.1 Profile template（不綁 UART port）
```yaml
profile_template:
  platform: bcm|prpl
  prompt_regex: ".*# $"
  login_regex: "(?mi)^login:\\s*$"
  password_regex: "(?mi)^password:\\s*$"
  post_login_cmd: "sh"
  ready_probe: "echo __READY__${nonce}; whoami"
  uart:
    baud: 115200
    data_bits: 8
    parity: N
    stop_bits: 1
    flow_control: none
    xonxoff: false
  uart_label: "115200_8N1"
```

### 7.2 Target binding（綁 by-id）
```yaml
target:
  com: COM0
  alias: project+1
  profile: prpl-template
  device_by_id: /dev/serial/by-id/usb-...
```

## 8. RAW 記錄格式
### 8.1 WAL（權威）
- `seq, mono_ts_ns, wall_ts, com, dir, source, cmd_id, len, crc32, payload_b64, loss_flag, meta`

### 8.2 text mirror
```text
<wall_ts> <mono_ns> <seq> <COM> <DIR> <SRC> <CMD_ID|-> <LEN> <CRC32> | <printable_payload>
```

## 9. 抗雜訊設計
- Layer A（RAW）：不清洗，保留原始位元組。
- Layer B（Result）：ANSI/BS/BEL/CR 清洗供顯示。
- Layer C（Decision）：`integrity != ok` 禁止自動下成功結論。

## 10. hotplug 行為
- 拔除：session -> `DETACHED`，不重試。
- 插入：同 by-id -> 自動掛回。
- 提供 `session clear` 清除失連 session。

## 11. minicom 相容
- broker 提供 `vtty-*`。
- `minicom` alias 改走 wrapper：優先連 broker PTY，不可用時回退 raw。
- 提供 `minicom-broker` / `minicom-raw`。

## 12. 測試與驗收
- 多 agent 併發下單寫入無交錯。
- seq 無重複、缺號可檢測。
- crc 驗證可用。
- gap/overflow 必標記 `partial/loss`。
- 多開 daemon 第二實例拒絕啟動。
