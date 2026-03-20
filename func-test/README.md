# func-test — serialwrap 功能測試框架

YAML 驅動的功能測試，專注於 human-agent 共用 UART 的穩定性驗證。

## 快速上手

```bash
# 執行全部測試
python3 func-test/runner.py

# 詳細輸出
python3 func-test/runner.py -v

# 只跑某個分類
python3 func-test/runner.py -c state-machine

# 只跑單一案例
python3 func-test/runner.py -t sm-01-attach-to-ready

# 重複執行（抓 race condition）
python3 func-test/runner.py -t rc-01-interactive-mode-switch -r 20

# 列出所有案例
python3 func-test/runner.py -l
```

## 目錄結構

```
func-test/
├── runner.py              # 測試執行器
├── README.md
├── lib/
│   ├── fake_target.py     # 可組態 PTY fake target
│   ├── daemon_harness.py  # Daemon 生命週期管理
│   ├── cli_client.py      # CLI 呼叫包裝
│   ├── console_client.py  # Console PTY 模擬
│   ├── expect_engine.py   # 期望語法比對引擎
│   └── yaml_loader.py     # YAML 載入
└── cases/
    ├── sm-*.yaml          # 狀態機轉移測試
    ├── ha-*.yaml          # Human-agent 共用測試
    ├── rc-*.yaml          # 競態條件測試
    ├── re-*.yaml          # 復原測試
    └── co-*.yaml          # Console I/O 測試
```

## YAML test case 格式

```yaml
meta:
  name: "測試名稱"
  category: "state-machine | human-agent | race-condition | recovery | console-io"
  severity: "critical | high | medium | low"
  description: "說明"
  tags: ["tag1", "tag2"]
  repeat: 1              # 預設重複次數

target:                  # fake target 行為
  platform: prpl
  boot_banner: "boot done\r\nroot@prplOS:/# "
  noise: { enabled: true, interval_ms: 50 }
  commands:
    default: "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "

profile:                 # serialwrap profile 覆寫
  prompt_regex: '...'
  timeout_s: 10

steps:                   # 測試步驟（循序執行）
  - action: wait_ready
  - action: cli
    argv: [...]
    expect: { ok: true }
```

## 支援的 action

| Action | 說明 |
|--------|------|
| `wait_ready` | 等待 session READY |
| `cli` | 執行 CLI 命令 |
| `wait_command_done` | 等待命令完成 |
| `parallel` | 併發執行子步驟 |
| `attach_console` | Attach human console |
| `console_write` | 寫入 console |
| `console_read` | 讀取 console 輸出 |
| `detach_console` | Detach console |
| `assert_state` | 驗證 session 狀態 |
| `assert_wal` | 驗證 WAL 記錄 |
| `sleep` | 注入延遲 |
| `inject_device_event` | 模擬裝置拔插 |
| `target_stop_responding` | Target 暫停回應 |
| `repeat` | 重複執行子步驟 |

## 與 unittest 的差異

| 面向 | tests/ (unittest) | func-test/ (YAML) |
|------|-------------------|-------------------|
| 格式 | Python | YAML |
| 重點 | 元件級單元測試 | 整合 / 場景測試 |
| 速度 | 快（< 1s） | 較慢（需啟動 daemon）|
| 適用 | CI 回歸 | Human-agent 互動驗證 |
| Race detection | 有限 | 可重複執行 |

## 研究報告

詳見 `docs/func-test/research-test.md`。
