# 功能測試研究報告

> 專案：serialwrap — 多 agent/human UART broker  
> 日期：2025-07  
> 目標：建立 YAML 驅動的功能測試框架，解決 human-agent 共用 UART 時的穩定性問題

---

## 1. 問題陳述

### 1.1 使用者反饋

- human minicom 輸入「常常卡到」— 疑似 console line buffering 或 interactive lease 時序問題
- 常常卡在「狀態錯誤」— 疑似 session state machine 的 TOCTOU（Time-of-Check-Time-of-Use）

### 1.2 現有測試覆蓋

| 測試檔案 | 測試數 | 涵蓋範圍 |
|----------|--------|---------|
| `test_session_bind.py` | 24 | Session 生命週期、binding、attach/detach、state transition、recovery、interactive lease |
| `test_agent_defer_tx.py` | 6 | UART bridge console：attach、line buffering、backspace echo、RX fanout |
| `test_auth.py` | 11 | Auth 解析：env file、session auth 解析 |
| `test_cli_daemon_start.py` | 6 | CLI daemon 啟動與整合 |
| `test_command_guard.py` | 4 | 命令過濾 |
| `test_config_profiles.py` | 6 | 設定檔與 profile 解析 |
| `test_daemon_lock.py` | 1 | Daemon lock |
| `test_login_fsm.py` | 3 | Login FSM |
| `test_multiagent_e2e.py` | 1 | E2E：5 agents × 3 rounds 併發命令 |
| `test_service_human_console.py` | 3 | Human console line mode 偵測 |
| `test_wal.py` | 1 | WAL |
| `test_alias_registry.py` | 1 | Alias registry |
| `test_runtime_paths.py` | 2 | Runtime path 解析 |
| **合計** | **69** | |

### 1.3 覆蓋缺口

現有測試主要是**白箱單元測試**與**一個 E2E 測試**。以下領域**完全沒有測試**：

| 缺口 | 嚴重度 | 說明 |
|------|--------|------|
| Interactive mode 啟用/停用競態 | **CRITICAL** | Human 命令完成 → lease 過期 → 下一個命令 buffering 正確性 |
| Console 偵測與輸入的併發 | **CRITICAL** | 同時 detach + input 的資料遺失/混模式 |
| Interactive owner snapshot TOCTOU | **CRITICAL** | arbiter 在 snapshot 與 check 之間讀到過期值 |
| Bridge 參照在 recovery 中的失效 | **HIGH** | 裝置重新插入，recovery thread 持有舊 bridge |
| Recovery thread 意外死亡 | **HIGH** | Session 永遠卡在 RECOVERING |
| Primary client 孤兒化 | **MEDIUM** | 所有 console detach 後 vtty_path 變 None |
| 背景 capture finalization 競態 | **MEDIUM** | `capture.maybe_finalize()` 期間 RX 資料仍在到達 |
| Human + Agent 交錯操作 | **HIGH** | Human 輸入中間 agent 命令進入 arbiter 的序列化 |

---

## 2. 測試方法論研究

### 2.1 白箱測試（White-box Testing）

**定義**：測試者具有程式碼內部結構的完整知識，基於程式碼邏輯設計測試案例。

**適用 serialwrap 的層面**：

| 技術 | 適用場景 | 例子 |
|------|---------|------|
| 語句覆蓋 | 確保所有程式碼路徑至少被執行一次 | login FSM 每個分支 |
| 分支覆蓋 | 每個條件的 True/False 都被測試 | `_refresh_interactive_locked()` 各種 lease 狀態 |
| 路徑覆蓋 | 組合多個分支的完整路徑 | attach → login → ready → execute → detach |
| 狀態轉移覆蓋 | 狀態機的每個轉移邊 | DETACHED→ATTACHING→ATTACHED→READY 所有邊 |
| 併發路徑測試 | 多執行緒的交錯排列 | human input + agent submit 的時序排列 |

**serialwrap 白箱測試重點**：
- `session_manager.py` 的 `self._lock` 保護範圍是否正確
- `uart_io.py` 的 `_state_lock` 與 `_interactive_owner` 的一致性
- `arbiter.py` worker thread 取出命令後的狀態檢查

### 2.2 黑箱測試（Black-box Testing）

**定義**：不依賴內部實作，純粹基於輸入/輸出規格設計測試。

**適用 serialwrap 的層面**：

| 技術 | 適用場景 | 例子 |
|------|---------|------|
| 等價分割 | 將輸入分組為等價類別 | 有效命令 vs 無效命令 vs 空命令 |
| 邊界值分析 | 測試邊界條件 | timeout 剛好到期、lease 剛好過期 |
| 決策表 | 多條件組合 | session state × device presence × interactive lease |
| 狀態轉移測試 | 從外部觀察狀態變化 | CLI `session list` 觀察狀態流轉 |
| 場景測試 | 端到端使用者情境 | human 登入 → 打 vim → agent 等待 → human 離開 → agent 執行 |

### 2.3 灰箱測試（Grey-box Testing）

**定義**：結合白箱與黑箱，測試者知道部分內部架構但透過外部介面操作。

**最適合 serialwrap 的原因**：
- Daemon 是獨立程序，透過 RPC（Unix socket）互動 → 自然是黑箱入口
- 但需要知道內部狀態機來設計有效的測試場景 → 需要白箱知識
- PTY fake target 可以精確控制時序 → 灰箱的核心優勢

### 2.4 狀態轉移測試（State Transition Testing）

serialwrap 的 session 狀態機是測試的核心。

#### 2.4.1 狀態圖

```
                 ┌──────────────────────────────────────────────┐
                 │                                              │
                 ▼                                              │
            DETACHED ──attach──▶ ATTACHING ──probe_ok──▶ READY │
                 ▲                   │                    │   │ │
                 │              probe_timeout             │   │ │
                 │                   │                    │   │ │
                 │                   ▼                    │   │ │
                 │              ATTACHED ◀─prompt_timeout─┘   │ │
                 │                   │                        │ │
                 │              recovery_ok                   │ │
                 │                   │                        │ │
                 │                   ▼                        │ │
                 │              READY ◀───────────────────────┘ │
                 │                                              │
                 │  READY ──reboot_cmd──▶ RECOVERING ──ok──▶ READY
                 │                            │                 │
                 │                       timeout/fail           │
                 │                            │                 │
                 └────────────────────────────┘─────────────────┘
```

#### 2.4.2 覆蓋層級

| 層級 | 說明 | 測試數量估算 |
|------|------|-------------|
| **0-switch** | 每個轉移邊至少測試一次 | ~10 cases |
| **1-switch** | 每對連續轉移至少測試一次 | ~25 cases |
| **n-switch** | 特定的長路徑序列 | ~10 cases（重點場景）|
| **無效轉移** | 在各狀態下嘗試不允許的操作 | ~15 cases |

### 2.5 併發測試策略

針對 serialwrap 的多執行緒架構：

| 策略 | 說明 | 適用場景 |
|------|------|---------|
| **壓力測試** | 大量併發操作 | 5+ agents 同時 submit |
| **時序注入** | 在關鍵點注入延遲 | 模擬 slow target 回應 |
| **交錯測試** | 控制操作順序 | human input → agent submit → human detach |
| **重複測試** | 相同測試執行多次 | 檢測 race condition 的非確定性失敗 |
| **Chaos 測試** | 隨機注入故障 | 裝置突然拔除、PTY 關閉 |

---

## 3. YAML 驅動測試框架設計

### 3.1 設計原則

1. **宣告式**：YAML 描述「做什麼」與「期望什麼」，不寫 Python
2. **可組合**：小步驟組合成複雜場景
3. **時序感知**：支援延遲、timeout、併發步驟
4. **可重複**：使用 PTY fake target，不依賴真實硬體
5. **獨立性**：每個 test case 自包含、啟動自己的 daemon
6. **human 模擬**：支援模擬 minicom/console 的字元輸入

### 3.2 YAML Schema

```yaml
# func-test/cases/<test-name>.yaml
---
meta:
  name: "測試名稱"
  category: "state-machine | human-agent | race-condition | recovery | ..."
  severity: "critical | high | medium | low"
  description: "測試描述"
  tags: ["interactive", "human", "agent", "recovery"]
  repeat: 1           # 重複次數（race condition 測試可設 10+）

# Fake target 行為定義
target:
  platform: prpl       # prpl | shell | passthrough
  boot_banner: "boot done\r\nroot@prplOS:/# "
  noise:
    enabled: true
    interval_ms: 50
    pattern: "KDBG:tick:{tick}\r\n"
  commands:
    # 預設：echo back + OK
    default: "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
    # 特定命令的自訂回應
    overrides:
      "reboot": { delay_ms: 500, response: "\r\n\r\nboot done\r\nroot@prplOS:/# " }
      "vim test.txt": { response: "", interactive: true }

# Profile 覆寫（會合併到預設 profile）
profile:
  prompt_regex: '(?m)^root@prplOS:.*# '
  ready_probe: 'echo __READY__${nonce}'
  login_regex: null    # null 表示不需要登入
  timeout_s: 10

# 測試步驟（循序執行，除非標記為 parallel）
steps:
  - action: wait_ready
    timeout_s: 15

  - action: cli
    argv: ["cmd", "submit", "--selector", "COM0", "--cmd", "echo hello", "--source", "agent:1"]
    expect:
      ok: true
      has_keys: ["cmd_id"]
    save_as: submit1

  - action: wait_command_done
    cmd_id: "{submit1.cmd_id}"
    timeout_s: 10

  - action: cli
    argv: ["cmd", "status", "--cmd-id", "{submit1.cmd_id}"]
    expect:
      "command.status": "done"
      "command.stdout": { contains: "RESULT:echo hello:OK" }

  # 併發步驟
  - action: parallel
    steps:
      - action: cli
        argv: ["cmd", "submit", "--selector", "COM0", "--cmd", "ls", "--source", "agent:1"]
      - action: cli
        argv: ["cmd", "submit", "--selector", "COM0", "--cmd", "pwd", "--source", "agent:2"]

  # Human console 模擬
  - action: attach_console
    selector: COM0
    label: "human-test"
    save_as: console1

  - action: console_write
    console_id: "{console1.client_id}"
    input: "echo from human\n"
    delay_ms: 100

  - action: console_read
    console_id: "{console1.client_id}"
    timeout_s: 5
    expect:
      contains: "RESULT:echo from human:OK"

  - action: detach_console
    console_id: "{console1.client_id}"

  # 狀態斷言
  - action: assert_state
    selector: COM0
    expect:
      state: "READY"
      interactive_lease: null

  # 延遲
  - action: sleep
    seconds: 0.5

  # WAL 驗證
  - action: assert_wal
    selector: COM0
    expect:
      min_tx_count: 3
      has_source: ["agent:1", "agent:2"]
```

### 3.3 Action 類型一覽

| Action | 說明 | 參數 |
|--------|------|------|
| `wait_ready` | 等待 session 進入 READY | `timeout_s`, `selector`（預設 COM0）|
| `cli` | 執行 CLI 命令，驗證 JSON 回應 | `argv`, `expect`, `save_as` |
| `wait_command_done` | 輪詢命令直到完成 | `cmd_id`, `timeout_s` |
| `parallel` | 併發執行多個子步驟 | `steps` |
| `attach_console` | 模擬 human attach console | `selector`, `label`, `save_as` |
| `console_write` | 寫入 console PTY | `console_id`, `input`, `delay_ms` |
| `console_read` | 讀取 console PTY 輸出 | `console_id`, `timeout_s`, `expect` |
| `detach_console` | 模擬 human detach | `console_id` |
| `assert_state` | 驗證 session 狀態 | `selector`, `expect` |
| `assert_wal` | 驗證 WAL 記錄 | `selector`, `expect` |
| `sleep` | 注入延遲 | `seconds` |
| `inject_device_event` | 模擬裝置拔插 | `event`（`remove`/`add`）|
| `target_stop_responding` | fake target 停止回應 | `duration_s` |
| `repeat` | 重複執行子步驟 | `count`, `steps` |

### 3.4 Expect 語法

```yaml
expect:
  # 精確匹配
  ok: true
  state: "READY"

  # 巢狀欄位（用 . 分隔）
  "command.status": "done"

  # 包含檢查
  "command.stdout": { contains: "hello" }

  # 正則匹配
  "command.stdout": { matches: "RESULT:.*:OK" }

  # 存在性
  has_keys: ["cmd_id", "session_id"]

  # 否定
  not: { state: "DETACHED" }

  # 數值比較
  min_tx_count: { gte: 3 }
```

---

## 4. 發現的 Bug 與競態條件

### 4.1 CRITICAL — Interactive mode 切換競態

**位置**：`uart_io.py:331`（`_handle_console_rx`）

**問題**：`_interactive_owner` 在 lock 外 snapshot，送出資料與 lease 撤銷之間有競態窗口。

```
Time 1: _handle_console_rx() snapshot owner = "human:cli-1"
Time 2: SessionManager.detach_console() 清除 _interactive_owner
Time 3: _handle_console_rx() 仍以 interactive 模式送出資料
Time 4: 下一次 console read，owner 為 None，落入 line buffering
→ 混合模式，協議混亂
```

**建議修正**：將 snapshot 與條件判斷放在同一個 `_state_lock` 區塊內。

### 4.2 CRITICAL — `_refresh_interactive_locked()` TOCTOU

**位置**：`session_manager.py:612`

**問題**：讀取 `bridge.snapshot()` 的 `interactive_owner` 後，bridge 狀態可能已經改變。

```
Time 1: snapshot.interactive_owner = "human:cli-1"（讀取）
Time 2: bridge detach_console() 清除 interactive_owner = None
Time 3: SessionManager 檢查 snapshot == lease.owner → 通過
→ Lease 驗證通過但 bridge 實際已不在 interactive 模式
```

### 4.3 HIGH — Recovery thread 無 try/except

**位置**：`session_manager.py:656`（`_spawn_reboot_recovery`）

**問題**：Recovery thread 是 daemon thread，沒有 try/except。若 `ensure_ready()` 拋出例外，thread 靜默死亡，session 永遠卡在 RECOVERING。

### 4.4 HIGH — `execute_command()` bridge 參照失效

**位置**：`session_manager.py:783-836`

**問題**：在 lock 內捕獲 `bridge` 參照後釋放 lock，後續使用 bridge 時裝置可能已經拔除，bridge 已 stop。

```
Time 1: execute_command() 取得 bridge ref，釋放 lock
Time 2: device callback → _detach_session_locked() → bridge.stop()
Time 3: execute_command() 呼叫 bridge.rx_snapshot_len()（bridge 已停止）
→ OSError 或未定義行為
```

### 4.5 HIGH — Arbiter 命令孤兒

**位置**：`arbiter.py:72-103`

**問題**：命令進入 queue 後 session 被 unregister，worker thread 持有舊 queue 參照，命令最終被拒絕但使用者已收到 `accepted`。

### 4.6 MEDIUM — Primary client 選擇非確定性

**位置**：`uart_io.py:474`

**問題**：`next(iter(self._clients.values()))` 依賴 dict 插入順序，多 console 時行為不可預測。

### 4.7 MEDIUM — Human reboot 狀態歧義

**位置**：`session_manager.py:738`

**問題**：回傳 `status: "interactive"` 但實際 session.state 是 `ATTACHED`，API 契約不清。

---

## 5. 建議的測試案例分類

### 5.1 Category: `state-machine`（狀態機轉移）

| 案例 | 覆蓋 |
|------|------|
| `sm-01-attach-to-ready.yaml` | DETACHED → ATTACHING → READY（正常路徑）|
| `sm-02-attach-timeout.yaml` | DETACHED → ATTACHING → ATTACHED（probe timeout）|
| `sm-03-ready-to-recovering.yaml` | READY → RECOVERING → READY（agent reboot）|
| `sm-04-device-unplug.yaml` | READY → DETACHED（裝置拔除）|
| `sm-05-invalid-transition.yaml` | 在各狀態下執行不允許的操作 |

### 5.2 Category: `human-agent`（human-agent 共用）

| 案例 | 覆蓋 |
|------|------|
| `ha-01-human-then-agent.yaml` | Human 輸入完成後 agent 命令 |
| `ha-02-agent-then-human.yaml` | Agent 命令進行中 human attach |
| `ha-03-interleaved.yaml` | Human 與 agent 交錯操作 |
| `ha-04-human-interactive-blocks-agent.yaml` | Human vim → agent 等待 → human 離開 |
| `ha-05-concurrent-submit.yaml` | Human 與 agent 同時 submit |

### 5.3 Category: `race-condition`（競態條件）

| 案例 | 覆蓋 |
|------|------|
| `rc-01-interactive-mode-switch.yaml` | Interactive lease 過期時的輸入切換 |
| `rc-02-detach-during-input.yaml` | Console detach 與輸入併發 |
| `rc-03-device-replug-during-attach.yaml` | Attach 期間裝置重新插入 |
| `rc-04-unregister-during-submit.yaml` | Session unregister 與命令提交併發 |

### 5.4 Category: `recovery`（復原）

| 案例 | 覆蓋 |
|------|------|
| `re-01-reboot-recovery-ok.yaml` | Reboot → recovery 成功 |
| `re-02-reboot-recovery-timeout.yaml` | Reboot → recovery 失敗 → ATTACHED |
| `re-03-device-replug-recovery.yaml` | 裝置拔插後自動 re-attach |

### 5.5 Category: `console-io`（Console I/O）

| 案例 | 覆蓋 |
|------|------|
| `co-01-line-buffering.yaml` | 正常行輸入與回顯 |
| `co-02-backspace-editing.yaml` | Backspace 編輯與回顯 |
| `co-03-multi-console.yaml` | 多個 console 同時 attach |
| `co-04-interactive-passthrough.yaml` | Interactive mode 的原始透傳 |

---

## 6. 實作架構

### 6.1 目錄結構

```
func-test/
├── runner.py              # 測試執行器主程式
├── lib/
│   ├── __init__.py
│   ├── fake_target.py     # PTY fake target（可組態）
│   ├── daemon_harness.py  # Daemon 生命週期管理
│   ├── cli_client.py      # CLI 執行包裝
│   ├── console_client.py  # Console PTY 模擬
│   ├── expect_engine.py   # Expect 語法比對引擎
│   └── yaml_loader.py     # YAML 載入與驗證
├── cases/
│   ├── sm-01-attach-to-ready.yaml
│   ├── ha-01-human-then-agent.yaml
│   └── ...
└── README.md              # 使用說明
```

### 6.2 執行方式

```bash
# 執行全部測試
python3 func-test/runner.py

# 執行特定分類
python3 func-test/runner.py --category state-machine

# 執行單一測試
python3 func-test/runner.py --case sm-01-attach-to-ready

# 重複執行（適合 race condition 測試）
python3 func-test/runner.py --case rc-01-interactive-mode-switch --repeat 20

# 詳細輸出
python3 func-test/runner.py --verbose

# 只列出測試案例
python3 func-test/runner.py --list
```

### 6.3 與既有 unittest 的關係

- `tests/` 下的 `unittest` 測試保持不變，適合 CI/CD 快速回歸
- `func-test/` 是**補充層**，專注於：
  - 時序敏感的 human-agent 互動
  - 需要精確控制 fake target 行為的場景
  - 需要重複執行來檢測 race condition 的測試
  - 非開發者也能讀懂的 YAML 測試描述

---

## 7. 參考資源

- [ToolOfCOM: YAML-driven serial protocol runtime](https://dev.to/_320e4b0df17e757ca44eb/i-built-a-runtime-framework-that-executes-serialtcp-protocols-from-yaml-no-more-upper-pc-coding-20ed)
- [yaml-testing-framework (PyPI)](https://pypi.org/project/yaml-testing-framework/)
- [Automated Hardware Testing with pytest](https://blog.golioth.io/automated-hardware-testing-using-pytest/)
- [State Transition Testing: Diagrams, Tables & Examples](https://keploy.io/blog/community/state-transition-testing)
- [Testing State Machines in Python](https://www.stickyminds.com/article/ensuring-reliable-cloud-applications-guide-testing-state-machines-python)

---

## 8. 結論

serialwrap 的核心問題在於**多執行緒、有狀態的 broker 架構**在 human 互動式操作下的穩定性。現有 69 個 unittest 提供了良好的**元件級**覆蓋，但嚴重缺乏：

1. **時序敏感的整合測試**：human 輸入 timing、interactive lease 切換
2. **併發競態測試**：多 thread 交錯場景的確定性重現
3. **錯誤恢復測試**：裝置拔插、recovery thread 失敗

YAML 驅動的功能測試框架可以用宣告式的方式填補這些缺口，同時讓非開發者也能理解與新增測試案例。
