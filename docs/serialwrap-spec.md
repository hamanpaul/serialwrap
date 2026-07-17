# serialwrap 規格書

## 1. 文件目的

> ⚠️ 本文件為**概覽**，非完整規格。canonical 規格已轉移到 `openspec/specs/*`（見下方各 capability 連結）；本文件僅保留高層脈絡，不再追蹤逐項行為。

本文件提供 `serialwrap` 主線的高層脈絡：讓單一 UART 可以安全地被多 Agent 與多個 minicom 共享，同時保留可追溯性、可診斷性與可恢復性。

本版重點：

- 完全移除 legacy `serialwrap_lib.py`
- 不再使用 tmux / shell marker
- target UART 只接收原始 command 或 raw keystrokes
- human console 改為 multi-console fan-out
- CLI / RPC 對齊新主線

## Canonical 規格（openspec/specs）

- 裝置 handoff（release/attach、#54）：[`openspec/specs/device-handoff/spec.md`](../openspec/specs/device-handoff/spec.md)
- MCU flash 端點（`/dev/ttyMCU`、FLASHING，#55）：[`openspec/specs/mcu-flash-broker/spec.md`](../openspec/specs/mcu-flash-broker/spec.md)
- command_capable readiness（#51）：[`openspec/specs/session-command-readiness/spec.md`](../openspec/specs/session-command-readiness/spec.md)
- 互動 session / soft-preempt（#53）：[`openspec/specs/session-interactive/spec.md`](../openspec/specs/session-interactive/spec.md)
- self-test：[`openspec/specs/session-selftest/spec.md`](../openspec/specs/session-selftest/spec.md)

## 2. 核心目標與不變量

### 2.1 目標

- 單 UART 單寫入仲裁
- 多 Agent / 多 minicom 可共享同一 COM
- command 可回傳結構化結果
- background / interactive / recover 有明確模型
- 裝置換 tty、bridge stale、target 無回應可被分類
- 只有 agent 明確 reboot 時才自動重新 login / READY

### 2.2 核心不變量

- 任一時刻只有一個真正的 UART writer
- 所有自動化寫入都經 broker
- RAW WAL 是權威證據
- human-visible 文字輸出不得污染 target
- target UART 上不得插入 broker marker

## 3. 系統元件

### 3.1 元件清單

- `serialwrapd.py`
- `sw_core/service.py`
- `sw_core/arbiter.py`
- `sw_core/session_manager.py`
- `sw_core/uart_io.py`
- `sw_core/login_fsm.py`
- `sw_core/device_watcher.py`
- `sw_core/wal.py`
- `sw_core/assets/tools/minicom_router.sh`

### 3.2 系統架構圖

```mermaid
flowchart LR
    subgraph C["Clients"]
        CLI["CLI"]
        MINI["minicom xN"]
    end

    CLI --> RPC["JSON-RPC"]
    MINI --> ROUTER["minicom_router"]
    ROUTER --> RPC
    RPC --> D["serialwrapd"]
    D --> A["Arbiter"]
    D --> S["SessionManager"]
    S --> B["UARTBridge"]
    B --> P["PTY xN"]
    B --> U["UART"]
    U --> T["Target"]
    B --> W["raw.wal.ndjson"]
    B --> M["raw.mirror.log"]
    A --> R["Command records"]

    classDef client fill:#e8f0fe,stroke:#355c9a,stroke-width:1px,color:#111;
    classDef core fill:#e8f5e9,stroke:#2f6b3f,stroke-width:1px,color:#111;
    classDef io fill:#fff3e0,stroke:#9a6b16,stroke-width:1px,color:#111;
    classDef log fill:#fce4ec,stroke:#9a3f64,stroke-width:1px,color:#111;

    class CLI,MINI,ROUTER client;
    class RPC,D,A,S,R core;
    class B,P,U,T io;
    class W,M log;
```

## 4. 啟動流程

### 4.1 啟動步驟

1. `serialwrap daemon start`
2. CLI 載入 runtime env（`SERIALWRAP_DAEMON_ENV_FILE` 或 legacy `~/OPI.env`）
3. 建立 runtime dirs / lock / socket
4. 載入 `profiles/*.yaml`
5. 建立 `SerialwrapService`
6. 啟動 `DeviceWatcher`
7. 首次掃描 by-id
8. 依裝置嘗試 attach
9. `UARTBridge.start()`
10. `ensure_ready()`
11. session 進 `READY`
12. CLI 可正式提交命令

### 4.2 啟動時序圖

```mermaid
sequenceDiagram
    participant User as User
    participant CLI as serialwrap
    participant D as serialwrapd
    participant W as DeviceWatcher
    participant S as SessionManager
    participant B as UARTBridge
    participant T as Target

    User->>CLI: daemon start
    CLI->>CLI: load runtime env (WAL_DIR etc.)
    CLI->>D: spawn
    D->>W: start
    W->>S: update devices
    S->>B: start bridge
    B->>T: blank command
    T-->>B: login / prompt
    S->>T: ready_probe
    T-->>S: nonce + prompt
    S-->>D: READY
    D-->>CLI: health ok
```

### 4.3 Client ↔ Daemon transport

- daemon 本體仍只 listen **Unix socket**
- CLI client 端可接受：
  - `--socket /tmp/serialwrap/serialwrapd.sock`
  - `--endpoint unix:///tmp/serialwrap/serialwrapd.sock`
  - `--endpoint tcp://host:port`
- `tcp://...` 的用途是讓 client 經由 **ssh-tunnel / socat / 隔離測試網路** 連到遠端 daemon；**不是**讓 daemon 直接 listen TCP
- `daemon start` 只支援本機 socket，不支援 `--endpoint`

## 5. Session / Console / Command 狀態

### 5.1 Session 狀態

- `DETACHED`
- `ATTACHING`
- `ATTACHED`
- `READY`
- `RECOVERING`

### 5.2 Console 模型

- 每個已掛上 bridge 的 session 都至少有一個 primary console PTY
- `session.console_attach` 會再建立一個專屬 PTY
- 所有 console 都收到同一份 RX fan-out

#### Raw Interactive（預設行為）

- **`console-attach` 在 `ATTACHED` 或 `READY` 狀態下，會自動授予第一個 human console raw interactive ownership**。minicom 連上即可使用方向鍵、Tab、ESC 序列等特殊按鍵，不需手動 `interactive-open`。
- Raw interactive 期間，所有 console bytes 透過 `UARTBridge.send_bytes()` 即時透傳到 UART，不做 line buffering 或 local echo。
- 第二個以後的 console 因為 interactive lease 已存在，仍走 line-buffer 模式。

#### Agent 命令搶佔（Suspend / Resume）

- Agent 提交 foreground 或 background 命令時，若 human 持有 interactive ownership，daemon 會 **暫時掛起（suspend）** human 的 raw mode：
  1. `bridge.suspend_interactive()` → 保存 owner、切到 deferred mode
  2. 執行 agent 命令
  3. `bridge.resume_interactive()` → 恢復 owner、flush deferred buffer 到 UART
- Human 在 agent 執行期間的按鍵輸入會累積在 deferred buffer（不做 local echo），agent 完成後一次性 flush 到 UART，由 target 自然回顯。
- Agent 命令**不再需要等待 human 關閉 minicom**；超時回 `SESSION_INTERACTIVE_BUSY` 的情境僅保留給 agent-vs-agent interactive lease 衝突。

#### Line-Buffer 模式

- 非 interactive owner 的 human console（第二個以後的 minicom）仍走 line-buffer 路徑。
- line-buffered human input 由 broker 提供本地回顯與基本 backspace 行編輯。
- 常見 human/minicom 互動式命令（如 `vim`、`top`、`less`、`menuconfig`）可自動升級為 human interactive ownership，避免被誤判為 prompt timeout 故障。

#### 其他

- 若 session 僅為 `ATTACHED`，`session.console_attach` 仍可使用，且該 console 會自動拿到 raw human ownership，方便手動登入或觀察 boot/log
- `platform=passthrough` 的 session attach 後不做 prompt/login/ready 探測，直接停在 `ATTACHED`，供未知設備走純 bridge/passthrough 路徑

### 5.3 Command record

每筆 command 至少包含：

- `cmd_id`
- `session_id`
- `command`
- `source`
- `mode`
- `execution_mode`
- `status`
- `stdout`
- `partial`
- `background_capture_id`
- `interactive_session_id`
- `recovery_action`
- `error_code`
- `started_at`
- `done_at`

## 6. Execution Modes

### 6.1 line

適用：

- `ifconfig`
- `iw dev wl0 link`
- `cat /proc/...`

流程：

1. queue 出隊
2. 送出原始 command + newline
3. 記錄 RX 起點 offset
4. 等 prompt regex 再次出現
5. 清除 echo / prompt
6. `stdout` 寫回 `command.get`

#### 命令限制

- 命令字串不得含有 `\n` 換行字元。`arbiter.submit()` 會在入口檢查，若偵測到換行則拒絕並回傳 `error_code: CMD_CONTAINS_NEWLINE`。
- 命令長度 > 4 KB 回 warning，> 16 KB 拒絕（`CMD_TOO_LONG`）。
- 長命令建議拆分為多步驟或使用 `file.push` 傳輸 script 後在 target 執行。

### 6.2 background

適用：

- `wl assoc scan`
- `cmd &`

流程：

1. queue 出隊
2. 送出原始 command
3. 等 prompt 回來
4. 開啟 background capture
5. 後續 chunk 由 `command.result_tail` 讀取

背景結果不是由 agent 直接 parse raw WAL，而是由 daemon 維護 capture 狀態與 chunk 邊界。

#### Background capture 建立前的 fallback

background 命令送出後，prompt 回來之前（capture 尚未建立），若 agent 立即呼叫 `command.result_tail` 或 `result.tail`，daemon 會走 `_bg_fallback_from_arbiter()` 路徑，回傳 pre-capture sentinel：

```json
{"ok":true,"from_seq":0,"last_seq":0,"chunks":[],"status":"pending","note":"capture not yet created"}
```

Agent 收到 sentinel 後應短暫 sleep 後重試，而非視為錯誤。

### 6.3 interactive

適用：

- `menuconfig`
- `vi`
- `top`

流程：

1. `session.interactive_open`
2. 建立 interactive lease
3. 設定 `UARTBridge.interactive_owner`
4. 透過 `session.interactive_send` 送 key / bytes
5. `session.interactive_status` 讀畫面
6. `session.interactive_close` 釋放 lease

若 `source=human:*` 的 line command 已送出但後續未回 prompt，daemon 會優先把該 console 升級成 human interactive，而不是直接觸發 recover。這條保護僅套用 human/minicom；agent foreground command 仍保留既有 prompt timeout / recover 路徑。

> bootloader recovery lease（`allow_attached=True`）、recovery lease 的 stash/restore、`MAX_RECOVERY_LEASE_S` clamp、`recovery_mode` 欄位語意、idle human lease soft-preempt 與 orphan console liveness 等逐項行為，canonical 規格見 [`openspec/specs/session-interactive/spec.md`](../openspec/specs/session-interactive/spec.md)（#53）。本概覽不再追蹤這些細節。

### 6.4 recover

適用：

- prompt timeout
- unmatched quote / continuation prompt
- interactive timeout
- target shell 卡住

升級順序固定：

1. `Ctrl-C`
2. `Ctrl-D`
3. 若仍無 prompt，session 降級為 `ATTACHED`

#### `_recover_after_failure` 語義

當前景命令因 `PROMPT_TIMEOUT` 失敗後，`_recover_after_failure()` 會嘗試透過 `Ctrl-C` / `Ctrl-D` 恢復 prompt。若成功恢復：

- 回傳 `ok: true`（表示 session 已回到可用狀態）
- 保留 `error_code: PROMPT_TIMEOUT_RECOVERED`
- 保留 `partial: true`（原始命令的輸出可能不完整）

這讓 caller 可以區分「命令失敗但 session 仍可用」與「session 完全失聯」。

#### 佇列 flush 語意（`FLUSHED_BY_RECOVERY`，#128）

session 因 recovery/re-attach 離開 `READY`（`_on_detached` → `arbiter.unregister_session`）時，該 session 的 PriorityQueue 會被丟棄；佇列中**尚未啟動**（`status=accepted`）的命令一律以終端態終結：

- `status: error`
- `error_code: FLUSHED_BY_RECOVERY`
- `done_at` 設為 flush 當下

語意：該命令**未被執行**（從未送進 UART），client 對這些 `cmd_id` 的 `command.get` 收到此終端態即應於 session 回 `READY` 後重送。in-flight（`running`/`interactive`）命令不重複標記，由 worker 以真實結果終結。flush 使記錄轉為可淘汰並即刻釋放 `CMD_PENDING_MAX` pending 額度——修掉 stale accepted 記錄永久佔額度、導致該 session 直到 daemon 重啟前一律 `SESSION_QUEUE_FULL` 的洩漏。

所有 detach 類路徑（含 recovery、`session clear`、device release、rebind、熱拔、re-attach 等 `_on_detached` 上游）皆以 `FLUSHED_BY_RECOVERY` 終結未啟動命令；daemon shutdown（`service.stop()`）則用 `FLUSHED_BY_SHUTDOWN`。兩者語意相同＝命令未執行、可於 session 回 `READY` 後重送。

### 6.5 長命令 keepalive hint

`command.submit` 支援選填的 `expected_duration_s` 參數，用於提示 broker 該命令預期的執行時間。機制包含：

1. **Foreground busy 保護**：前景命令執行期間，prompt timeout 暫停，避免誤判為 session 失聯。
2. **Expected duration hint**：在 `expected_duration_s` 期間內不觸發 prompt 超時警告。
3. **Output-based keepalive**：監控 UART RX 活動，若有任何輸出（如測試進度、編譯訊息），自動重置靜默計時器，延長等待。

範例：

```json
{"method":"command.submit","params":{
  "selector":"COM0",
  "cmd":"python3 -m unittest discover -s tests -v",
  "source":"agent:ci",
  "mode":"line",
  "timeout_s":300,
  "expected_duration_s":120
}}
```

適用場景：`apt upgrade`、`make`、`python -m unittest`、`opkg install` 等長時間但有間歇輸出的命令。

詳見設計文件：[`docs/design-heartbeat-keepalive.md`](./design-heartbeat-keepalive.md)。

## 7. 呼叫流程圖

```mermaid
flowchart TD
    SUBMIT["submit command"] --> MODE{"mode"}

    MODE -->|line| LQ["queue"]
    LQ --> LS["send raw command"]
    LS --> LP["wait prompt"]
    LP --> LO["trim echo / prompt"]
    LO --> LR["command.get stdout"]

    MODE -->|background| BQ["queue"]
    BQ --> BS["send raw command"]
    BS --> BP["prompt returns"]
    BP --> BC["background capture"]
    BC --> BR["command.result_tail"]

    MODE -->|interactive| IO["interactive_open"]
    IO --> IK["interactive_send"]
    IK --> IS["interactive_status"]
    IS --> IC["interactive_close"]

    LP --> TO["timeout"]
    TO --> RC["Ctrl-C"]
    RC --> RD["Ctrl-D"]
    RD --> RA["mark ATTACHED"]
```

## 8. Device 變更與 reattach

### 8.1 掃描來源

- 來源：`/dev/serial/by-id`
- key：`device_by_id`
- 值：`real_path`

### 8.2 重新 attach 條件

以下任一條件成立都視為需要重建 bridge：

- `device_by_id` 消失
- `device_by_id` 重新出現
- `device_by_id` 不變但 `real_path` 改變

### 8.3 預期行為

- bridge 停止
- session 先進 `DETACHED`
- 重新 `_attach_by_id`
- 若看到 shell prompt，送 `ready_probe` 後回 `READY`
- 若沒看到 prompt，保留 bridge 並停在 `ATTACHED`

### 8.4 COM 編號確定性 rank（#100）

dynamic 自動偵測 session 的 COM 編號**依裝置 by-id 字典序確定性分配**（排序鍵 `device_sort_key(by_id, by_path)`：以 by-id 為主）：

- daemon startup 在 spawn 並發 attach threads 之前（`SerialwrapService.start()` 於 `update_devices` / `bootstrap_attach` 之前呼叫 `prepare_dynamic_rank`），對「當下在線的 dynamic 裝置」一次排序配好 COM rank，因此 restart 後 COM↔板對應穩定不變。
- rank 作用域**只限 dynamic**：explicit YAML `targets`、`session.bind` / `_binding_overrides`、RELEASED 裝置排除在 pool 外，COM 由其權威來源決定。
- startup 預配（`_pending_com`）的 lifecycle：對應裝置離線時，`update_devices` 會 prune 其 pending，COM 號隨即可被回收（`_next_dynamic_com` 純掃描最低空號）。
- runtime hotplug：不同 by-id 繼承空出的 DETACHED 槽；同 by-id 重接拿回原槽；active session 的 COM 名在 daemon 存活期間不變。

**by-path tiebreak（TODO）**：`device_sort_key` 已接受 by-path 參數作為同 by-id（如同款 CH340）的次序 tiebreak，但 end-to-end 完整支援為 **TODO**——需 `DeviceInfo.by_path` 欄位接上資料來源，在此之前 `_by_path_for` 一律回 None、rank 僅依 by-id。

**on-demand `session renumber` 已 defer 至 follow-up**：把執行期漂移的 COM snap 回 sorted by-id 序的 RPC/CLI，因強制重編 active session 牽動 bridge callback / flash state / lease reverse-link，須改以「拆 bridge → 改號 → 重 attach」另案重做，本版不含。

### 8.5 同機多開（two-reader）偵測（#101）

純被動、on-demand 偵測同機是否有一個以上 `serialwrapd`（不終止任何 daemon、不退讓、無背景週期掃描）。module-level `detect_multi_open(proc_root, tty_paths)` 掃 `/proc/*/cmdline` 找 `serialwrapd`、best-effort 讀 `/proc/<pid>/fd` 找 tty 持有者。兩個 surface：

- **`serialwrap doctor`** → `single_daemon` 檢查項（daemon-less，回 `{check, ok, detail, fix}`）：多開時 `ok=false`。
- **`serialwrap daemon status`**（RPC `health.status`）→ 回應加欄位：
  - `multi_open`（bool）。
  - `foreign_holders`（`{tty_real_path: pid}`）：持有目前 attach 中 tty 的 pid。
  - `multi_open_detail`：`{"daemons": [{"pid": N}, ...], "holders_status": "ok" | "permission" | "unknown"}`。`holders_status` 在跨 uid 讀不到 `/proc/<pid>/fd` 時降級為 `permission`、procfs 不可用時為 `unknown`，此降級資訊本身即為輸出契約的一部分。

## 9. self_test 與 recover

### 9.1 `session.self_test`

`session.self_test` 對指定 session 做唯讀 readiness 診斷，回傳裝置/bridge/target 的健康分類（如 `OK` / `DEVICE_MISSING` / `BRIDGE_DOWN` / `BOOTLOADER` / `ATTACHED_NOT_READY` 等），並隨回應帶上 lease/handoff context（如 `human_active`、`recovery_mode`、`command_capable`、RELEASED 可收回性）。

> self_test 的完整輸出分類、欄位定義、判斷順序、collaborative monitoring（預設 `strict_human_lock=false` 與 probe 期間 suspend/resume）、`BOOTLOADER`/`command_capable`/RELEASED handoff 等逐項行為，canonical 規格見 [`openspec/specs/session-selftest/spec.md`](../openspec/specs/session-selftest/spec.md)（#51 / #54 / #55）。本概覽不再追蹤這些細節。

### 9.2 `session.recover`

若 session 狀態為 `ATTACHED` 且 bridge 仍在：

- 直接對現有 bridge 做 re-probe
- 成功則回到 `READY`
- 失敗則保留 `ATTACHED`，並把失敗原因留在 `last_error`

若 session 狀態為 `READY` 且 bridge 仍有可用：

1. 送 `Ctrl-C`
2. 等 prompt
3. 失敗則送 `Ctrl-D`
4. 再失敗則把 session 降級成 `ATTACHED`

若 session 已無 bridge 但裝置還在：

- 直接走 reattach
- attach 流程只做被動 prompt probe，不自動 login

## 10. Logging 與輸出層

### 10.1 RAW WAL

檔案：

- `raw.wal.ndjson`

內容：

- `seq`
- `mono_ts_ns`
- `wall_ts`
- `com`
- `dir`
- `source`
- `cmd_id`
- `len`
- `crc32`
- `payload_b64`
- `loss_flag`
- `meta`

### 10.2 Mirror

檔案：

- `raw.mirror.log`

內容：

- printable stream
- 接近透明 console 視角
- 不附帶每行 metadata prefix

預設目錄是 `/tmp/serialwrap/wal/`。若只想改 WAL / mirror log 的位置，可設定 `SERIALWRAP_WAL_DIR`（例如 `~/b-log`）；這不會改動 daemon socket / lock 的 runtime 目錄。

### 10.4 Agent per-session capture

Agent 可透過 `session.log_start` / `session.log_stop` 對特定 session 啟停純文字 RX capture：

- 日誌寫入 `{log_dir}/{COM}_{YYMMDD}-{HHMMSS}.log`
- `log_dir` 優先序：per-target `log_dir` > per-profile `log_dir` > YAML `defaults.log_dir` > `SERIALWRAP_LOG_DIR` env > `~/b-log`
- 同一 session 同時最多一個 active capture
- session detach 時自動停止 capture
- WAL 是 always-on 審計記錄，agent log 是 on-demand focused capture，兩者互補

### 10.3 Command result

來源：

- line mode：`command.get.stdout`
- background mode：`command.result_tail`（含 `from_chunk` / `next_chunk` 分頁）
- interactive mode：`interactive_status.screen`

background mode 的 `command.result_tail` 在 capture 尚未建立時，會回傳 `from_seq=0, last_seq=0` sentinel（詳見 6.2 fallback 說明）。

## 10.5 檔案傳輸（file.push / file.pull）

內建的 UART 檔案傳輸 primitive，透過 base64 分段傳輸與 md5 校驗，取代不可靠的 inline base64 / heredoc workaround。

### file.push（host → target）

1. 讀取本地檔案，分割為 chunk（預設 2KB）
2. 每個 chunk：base64 編碼後送出 `echo '<b64>' | base64 -d >> /tmp/.sw_upload_<id>`
3. 所有 chunk 送完後：校驗 checksum（`md5sum`）
4. 將暫存檔 rename 到最終路徑

### file.pull（target → host）

1. 在 target 執行 `base64 < /path/to/file`
2. 透過 UART 分段擷取輸出
3. 解碼並寫入本地
4. 校驗 checksum

### Remote endpoint 模式下的語意

- `file.push` / `file.pull` 中的「host」是 **daemon 所在主機**
- 若 agent 透過 `--endpoint tcp://...` 連到遠端 daemon，`local_path` 也是 **遠端 host/container** 的路徑，不是 agent 自己機器上的路徑
- 若需要傳輸 agent 本機檔案到遠端 host，應先透過 ssh/scp/rsync 把檔案送到 daemon host，再呼叫 `file.push`

### 前置條件

- Session 必須處於 `READY` 狀態
- Target 必須有 `base64`、`md5sum` 或 `sha256sum`

詳見設計文件：[`docs/design-file-transfer.md`](./design-file-transfer.md)。

## 11. Public Interface

### 11.1 CLI

- 全域 transport 參數：
  - `--socket <path>`（預設，本機 Unix socket）
  - `--endpoint <endpoint>`（優先於 `--socket`，支援 `unix://` / `tcp://`）
- `serialwrap daemon start|stop|status`
- `serialwrap device list`
- `serialwrap session list|bind|attach|clear`
- `serialwrap session self-test|recover`
- `serialwrap session console-attach|console-detach|console-list`
- `serialwrap session interactive-open|interactive-send|interactive-status|interactive-close`
- `serialwrap session log-start|log-stop|log-status`
- `serialwrap alias list|set|assign|unassign`
- `serialwrap cmd submit|status|result-tail|cancel`
- `serialwrap file push|pull`
- `serialwrap log tail-raw|tail-text`
- `serialwrap stream tail` (legacy alias，對應 `result.tail`)
- `serialwrap wal export`
- `serialwrap session pin|unpin`

#### session pin / unpin（動態裝置 profile 持久化，#95）

`serialwrap session pin --selector <COM|alias|by-id|by-path> --profile <name>` 把裝置釘到指定 profile（最高優先，繞過動態偵測，跨重啟保留）；`serialwrap session unpin --selector <...>` 解除 pin（保留自動 sticky）。

- **同款晶片（如 CH340）by-id 相同時，務必以 `/dev/serial/by-path/...` 當 selector**，避免 pin/sticky 張冠李戴（與既有 binding 規範一致）。
- profile 解析優先序：pin > sticky（偵測達 READY 後自動記住）> 動態偵測 > others-template fallback。
- `session list` 的 `profile_source` 欄位顯示來源：`pin` / `sticky` / `detected` / `fallback` / `yaml-target`。
- 錯誤碼：`UNKNOWN_PROFILE`（profile 名不存在）、`PROFILE_IS_EXPLICIT`（對 YAML explicit-target 裝置 pin/unpin）、`DEVICE_NOT_FOUND`（selector 解析不到裝置）、`INVALID_ARGS`（缺 selector/profile）。
- **生效時機**：pin/unpin 寫入後不主動重新 attach；對已存在的 session，**下次 daemon 重啟生效**（重啟時 session 重建走動態偵測路徑才重讀 pin/sticky）。執行期 `clear`/`attach` 沿用既有 session 的 profile、不重選。

### 11.2 RPC

- `health.ping`
- `health.status`
- `device.list`
- `session.list`
- `session.get_state`
- `session.clear`
- `session.bind`
- `session.attach`
- `session.self_test`
- `session.recover`
- `session.console_attach`
- `session.console_detach`
- `session.console_list`
- `session.interactive_open`
- `session.interactive_send`
- `session.interactive_status`
- `session.interactive_close`
- `session.log_start`
- `session.log_stop`
- `session.log_status`
- `session.pin`
- `session.unpin`
- `alias.list`
- `alias.set`
- `alias.assign`
- `alias.unassign`
- `command.submit`
- `command.get`
- `command.cancel`
- `command.result_tail`
- `file.push`
- `file.pull`
- `log.tail_raw`
- `log.tail_text`
- `wal.range`

> **`session.attach` 回傳契約（#94）**：對 command-capable session，未進入 `READY` 的 bridge-present 狀態一律回 `ok:false` + 頂層 `error_code`（開機窗／probe 未成功→`PROMPT_UNAVAILABLE`／`session.last_error`；`FLASHING`→`FLASHING_BUSY`；`RECOVERING`→`SESSION_RECOVERING`；`human:*` interactive lease 佔用而不 probe→`last_error`；其餘→`NOT_READY`），CLI exit code = `2` 並在 stderr 印一行具體錯誤。**回 `ok:true` 的情形**：達 `READY`（可下命令；human lease 下與人交錯）、`ATTACHING`（fresh attach 進行中／async accept）、`RELEASED`（裝置已 release／`released_by_id`，回 `released:true` + `recommended_action=device_attach`，屬保護性 no-op、不建立 bridge，需 `device attach` 重取）、`platform=passthrough`（非 command-capable、`ready_probe` 空、停 `ATTACHED` 即成功）。此為「誠實回報未達 READY、可重試（daemon 有界自動重探）、非致命」契約，供上層 driver 據以 retry/wait。

## 12. `minicom_router.sh` 行為

1. 確認 daemon 存活，必要時自動啟動
2. 找到 selector 對應 session
3. session 既非 `READY` 也非 `ATTACHED`，且允許自動 attach 時，先 `session attach`
4. `session console-attach`
5. 以回傳的 PTY 啟動 `minicom`
6. minicom 結束後 `session console-detach`

`minicom_router.sh` 不再依賴 session 單一 `vtty` 當唯一入口。

預設 transcript 走 minicom 內建 `-C` capturefile（乾淨序列 log，不含 minicom UI）；可設 `MINICOM_CAPTURE_MODE=script`（或舊式 `MINICOM_CAPTURE_WRAPPER=1`）改用 `script -qef` 包一層 PTY 來保留含完整終端畫面的 transcript。後者可能增加 human 體感延遲。

## 13. Profile 規格

### 13.1 YAML 結構

Profile YAML 由三個頂層區段組成（`targets` 為可選）：

```yaml
defaults:
  log_dir: "~/b-log"       # 全域 agent log 預設目錄
  max_sessions: 16         # 動態 session 上限（預設 16）

profiles:
  <template-name>:
    platform: ...
    prompt_regex: ...
    # ... 其餘 template 欄位

# targets 區段為可選：省略 → 全部走動態偵測
targets:
  - act_no: 1
    com: COM1
    alias: <alias>
    profile: <template-name>
    device_by_id: /dev/serial/by-path/...  # 或 /dev/serial/by-id/...
```

`defaults` 區段支援：
- `log_dir`：agent log 寫入目錄，若未設定則 fallback 到 `SERIALWRAP_LOG_DIR` env 或 `~/b-log`
- `max_sessions`：動態 session 上限，預設 16，超過時忽略新裝置

### 13.2 Template 欄位

關鍵欄位：

- `platform`：`shell` / `prpl` / `bcm` / `passthrough`
- `prompt_regex`
- `login_regex`
- `password_regex`
- `username`
- `user_env`
- `pass_env`
- `env_file`
- `post_login_cmd`：登入後自動送出的命令（用於 `bcm` 平台兩階段切換）
- `ready_probe`
- `timeout_s`：登入等待超時（預設 10s）
- `quiet_window_s`：background capture idle finalize 安靜等待時間
- `hard_timeout_s`：命令硬超時
- `log_dir`：per-profile agent log 目錄，覆寫 `defaults.log_dir`
- `uart.*`：序列埠參數（baud、data_bits、parity、stop_bits、flow_control、xonxoff）
- `bootloader_prompts`：bootloader prompt 的 regex 清單（YAML list，內部以 `tuple[str, ...]` 儲存，預設 `()`）。僅接受 `str` 元素；int/null/dict 等非字串元素會被過濾。此欄位供 `self_test` 的 `BOOTLOADER` 分類與 `interactive_open(..., allow_attached=True)` recovery gate 使用；只在 `ATTACHED` 狀態下比對 RX tail 最後一個非空行，並優先於 `ATTACHED_NOT_READY` fallback。支援 Python `re` 語法。範例：
  ```yaml
  bootloader_prompts:
    - "^CFE> $"       # Broadcom CFE bootloader
    - "^=> $"         # U-Boot 標準 prompt
    - "^BCM\\d+>> $"  # Broadcom BCM 系列 bootloader
  ```

### 13.3 Platform 行為

**`platform=shell`**：generic Linux login。可使用 `user_env` / `pass_env` 做帳密。帳密解析採用 **per-session 隔離**：在每次 session attach 時，`sw_core/auth.py` 的 `resolve_session_auth()` 會從 `env_file` 解析帳密（純 Python 解析，不 fork shell），不同 COM / template 可用不同的 `env_file` 指向不同帳密。查找順序為：`env_file` 內的 key → `os.environ` fallback → `username` 欄位。相對路徑會以該 YAML 所在目錄解析。若 profile 沒有宣告 `env_file`，帳密仍從 daemon 的 `os.environ` 讀取（向後相容）。若裝置 login prompt 會帶 hostname（例如 `orangepi3 login:`），建議 `login_regex` 使用 `(?mi)^.*login:\\s*$`。若裝置已自動登入並直接出現 prompt，daemon 會略過 login 流程，直接做 `ready_probe`。

**`platform=bcm`**：Broadcom 原生平台（如 BCM968575）。登入後 target 進入 BCM CLI shell（`>`），需再執行 `post_login_cmd`（通常是 `sh`）才進到 Linux shell（`#`）。`timeout_s` 建議加大（15s+），因 Broadcom 登入流程較慢。

**`platform=prpl`**：prplOS 平台。`prompt_regex` 應匹配 prompt prefix，例如 `(?m)^root@prplOS:.*# `。不依賴行尾錨點，以適應 prompt 後接 driver / kernel log 的情境。`ready_probe` 保持最小 `echo __READY__${nonce}`。

**`platform=passthrough`**：不做 `ready_probe` / login 流程。daemon 只建立 UART bridge 與 console PTY，session 停在 `ATTACHED`，由 human console 或後續人工判斷設備型態。

### 13.4 裝置識別

`device_by_id` 支援兩種穩定路徑：

- `/dev/serial/by-id/...`：基於 USB descriptor，但若多張板使用同款晶片（如 CH340）會完全相同
- `/dev/serial/by-path/...`：基於物理 USB port 路徑，不隨列舉順序變動，同款晶片也能區分

若遇到同晶片無法區分的情境，建議改用 `by-path`。

### 13.5 Auto-detect Template（自動偵測）

當 DeviceWatcher 偵測到新 UART 裝置且無任何 explicit target 綁定匹配時，daemon 進入自動偵測流程。

**觸發條件**：
- 裝置的 `by_id` 路徑不在任何已建立 session 的 `device_by_id` 中
- 也沒有 DETACHED session 可重新綁定
- `templates` 列表非空（由 `load_profiles()` 從 YAML `profiles` 區段載入）
- 現有 session 總數未達 `max_sessions`

**偵測流程**（`login_fsm.detect_template()`）：

1. 用預設 UART 參數（115200/8N1）開啟臨時 probe bridge
2. `bridge.clear_rx_buffer()` + `bridge.send_command("")`（送 `\r`）
3. 等待 `probe_timeout_s`（預設 3 秒）收集 UART 輸出
4. 取得 `bridge.rx_tail()` snapshot
5. 依序嘗試各 template（passthrough 排最後）：
   - **prompt_regex 匹配** → 立即選定此 template
   - **login_regex 匹配** → 記錄為候選
6. 若有 login_regex 候選 → 選定第一個匹配的 template
7. 全不匹配 → 回傳 `None`（caller 使用 passthrough fallback）

**動態 session 建立**（`session_manager._attach_by_id_dynamic()`）：

1. 偵測成功或 fallback 後，呼叫 `_session_from_template()` 建立新 session
2. `_next_dynamic_com()` 分配 COM 編號（COM0, COM1, ...，跳過已用的）
3. session_id 格式為 `{profile_name}:{COM}`
4. 關閉 probe bridge，用偵測到的 template 的 UART 參數重新開啟正式 bridge
5. 繼續 login/ready 流程

**設計決策**：
- 偵測結果不持久化：每次裝置出現都重新偵測
- passthrough 永遠是最後 fallback，不會被 `prompt_regex: ".*"` 搶先匹配
- profiles 定義順序決定偵測優先級 → 把 specific（prpl/bcm）排在 generic（shell）前面
- `max_sessions` 預設 16，超過則 log warning 並忽略新裝置

## 14. 驗收標準

- line mode 命令完成後 `stdout` 正確回填
- 含 `\n` 的命令被 `arbiter.submit()` 拒絕，回傳 `CMD_CONTAINS_NEWLINE`
- background mode 可用 `result-tail` 取得後續 chunk
- background capture 建立前的 `result-tail` 回傳 `from_seq=0, last_seq=0` sentinel
- prompt timeout 後 `result-tail` 仍可查 terminal status / error_code 與已緩衝 chunk
- `_recover_after_failure` 成功恢復 prompt 時回 `ok: true`（含 `PROMPT_TIMEOUT_RECOVERED`）
- `expected_duration_s` 可延長前景命令 keepalive 等待
- interactive lease 可建立、送 key、關閉
- 多 console attach 可同時收到 RX
- human input 在 line-buffer 模式下經 broker 排隊；raw interactive 模式直接透傳 UART
- 裝置 real_path 變更時會 reattach
- `self_test` 可區分主要故障類型
- `recover` 會先對 `ATTACHED` bridge 做 re-probe；`READY` 才走 `Ctrl-C -> Ctrl-D`
- agent 明確 reboot 後可重新回 `READY`
- `file.push` / `file.pull` 可完成 base64 分段傳輸與 md5 校驗
- README / spec / CLI 命名一致
- Docker smoke flow 可用同一個 image 起兩個 container，並透過 `--endpoint tcp://<container-name>:7777` 成功完成 `daemon status` / `session list` / `cmd submit`

## 15. 使用建議

- human / operator：
  - `session self-test`
  - `session recover`
  - `minicom`
- agent：
  - `command.submit`（長命令加 `expected_duration_s`）
  - `command.get`
  - `command.result_tail`
  - `file.push` / `file.pull`（取代 inline base64 / heredoc workaround）
  - interactive 類走 `session.interactive_*`
- 稽核：
  - `log tail-raw`
  - `wal export`

## 16. 非目標

以下不屬於本版：

- 再次引入 tmux / pane marker
- 允許 broker 外部直接寫 `/dev/ttyUSB*`
- 將 raw WAL 降格為非權威資料
