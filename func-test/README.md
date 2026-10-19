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

## Bridge 模式與執行證據

`pty` 模式維持既有本機 FakeTarget 路徑；`bridge` 模式則由獨立的 `uart-stdio-bridge` 接上本機 process 或 Docker DUT。執行 bridge 前，必須提供符合 Task 8 資格檢查的 selftest 報告：

```bash
python3 func-test/runner.py \
  --mode bridge \
  --case sm-01-attach-to-ready \
  --selftest-report /path/to/selftest-report.json \
  --report-dir /path/to/report-root

# 同一批案例各跑一次 FakeTarget PTY 與外部 bridge
python3 func-test/runner.py \
  --mode both \
  --selftest-report /path/to/selftest-report.json
```

bridge 執行只會把 daemon 的 state、run、WAL、config、log、events 與裝置索引目錄放在該次 harness 的暫存根目錄。`SERIALWRAP_CLI_EXE` 與 `SERIALWRAP_DAEMON_EXE` 可選擇要測的 CLI 與 daemon；兩者不會傳給隔離 daemon 作為產品設定。Windows bridge 需明確指定兩個 com0com 端點：

```powershell
python func-test/runner.py --mode bridge `
  --selftest-report C:\reports\selftest-report.json `
  --daemon-com COM20 --bridge-com COM21 `
  --transport-mode virtual_com0com
```

執行結果寫到 `<report-dir>/<case>/<mode>/<run-index>/`，並在 report root 產生 `func-test-report.json`。每次 run 包含 `manifest.json`、`cli.ndjson`、`daemon.log` 與隔離 WAL 複本；bridge 模式另外包含 wire、control、DUT stderr、correlation 與 `comparison.json`。不存在的證據檔在 manifest 中記為 `null`，不建立空檔冒充證據。`tests/acceptance/cases.yaml` 尚未建立時，`cases_yaml_sha256` 為 `null` 且 `gate_eligible` 為 `false`。工具自測或核准 revision 不合格時，bridge run 會以 `INSUFFICIENT_EVIDENCE` 結束，不會標為 PASS。

## 目錄結構

```
func-test/
├── runner.py              # 測試執行器
├── README.md
├── lib/
│   ├── fake_target.py      # 可組態 PTY fake target
│   ├── daemon_harness.py   # 隔離 daemon 與 DUT 的生命週期管理
│   ├── bridge_session.py   # 外部 bridge、settle 與 wire tail
│   ├── correlation.py      # 每步驟的操作與證據邊界
│   ├── artifact_identity.py # CLI 與 daemon artifact 身分
│   ├── target_config.py    # 跨平台 DUT 組態匯入
│   ├── cli_client.py       # CLI 呼叫包裝
│   ├── console_client.py   # POSIX PTY 與 Windows Telnet console
│   ├── expect_engine.py    # 期望語法比對引擎
│   └── yaml_loader.py      # YAML 載入
└── cases/
    ├── sm-*.yaml          # 狀態機轉移測試
    ├── ha-*.yaml          # Human-agent 共用測試
    ├── rc-*.yaml          # 競態條件測試
    ├── re-*.yaml          # 復原測試
    └── co-*.yaml          # Console I/O 測試

realhw/
├── deployed_content.py    # 實際部署內容與 daemon image 量測
└── telnet_client.py       # Windows console 共用的 Telnet codec
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

bridge:                   # 外部 bridge 模式的適用性與證據比較
  applicable: true
  compare: full            # full | none | until_op:<op_id>
  run_args: []             # 僅接受 bridge run 的白名單參數
  secrets_from_env: []     # 可選；列出需遮蔽的環境變數名稱
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
| `shell` | 在 harness 根目錄與隔離環境中執行 shell 命令 |
| `target_emit` | 由 DUT 輸出指定 bytes |
| `wait_for_file` | 等待指定檔案出現 |
| `write_file` | 展開變數後以 UTF-8 寫入檔案 |
| `wait_state` | 輪詢 session，直到進入指定狀態 |

`console_write.input_raw` 依 Latin-1 字元值逐 byte 傳送，適合 ESC、Tab 等特殊按鍵；超出 U+00FF 的字元會使該步驟失敗。

## 與 unittest 的差異

| 面向 | tests/ (unittest) | func-test/ (YAML) |
|------|-------------------|-------------------|
| 格式 | Python | YAML |
| 重點 | 元件級單元測試 | 整合 / 場景測試 |
| 速度 | 快（< 1s） | 較慢（需啟動 daemon）|
| 適用 | CI 回歸 | Human-agent 互動驗證 |
| Race detection | 有限 | 可重複執行 |

## Remote Support Docker test flow

除了本機 PTY / YAML 功能測試外，repo 也提供一個 **雙 container smoke flow**，用來驗證 remote-support 的 `--endpoint tcp://...` 能否跨 container 存取遠端 daemon。

### 目的

- **Container A**：跑 fake target + `serialwrapd` + `socat`
- **Container B**：只當 remote client，透過 `serialwrap --endpoint tcp://<container-a>:7777 ...` 存取 A

### 相關檔案

| 檔案 | 用途 |
|------|------|
| `Dockerfile` | 建立可執行 serialwrap 的 image |
| `tools/docker/remote_lab.py` | 在 container A 內啟動 fake target / daemon / socat |
| `tools/docker/remote_smoke.sh` | 在 host 端 build image、建立 network、起兩個 container 並驗證 remote flow |

### 執行方式

```bash
./tools/docker/remote_smoke.sh
```

預期流程：

1. build `serialwrap:remote-smoke` image
2. 建立獨立 bridge network
3. 起 `sw-remote-a-*` container，內含 fake target + daemon + TCP endpoint
4. 再起另一個臨時 client container，依序驗證：
   - `serialwrap --endpoint tcp://<remote>:7777 daemon status`
   - `serialwrap --endpoint tcp://<remote>:7777 session list`
   - `serialwrap --endpoint tcp://<remote>:7777 cmd submit --selector COM0 --cmd 'uname -a'`
   - `serialwrap --endpoint tcp://<remote>:7777 cmd status --cmd-id <id>`

### Docker 網路原則（避免 IP/MAC 衝突）

- 一律使用 **user-defined bridge network**
- **不要指定固定 IP**
- **不要指定固定 MAC**
- 兩個 container 一律用 **Docker DNS 名稱**（例如 `sw-remote-a-12345`）互連
- `remote_lab.py` 在 Docker smoke 模式會讓 `socat` bind `0.0.0.0`，但 **只存在於隔離的 bridge network 中**，不 publish 到 host，也不是 production 建議做法

### 與正式 remote-support 的差異

- **正式環境**：FAE 主機應只 `bind=127.0.0.1`，再由 RD 透過 ssh-tunnel 連接
- **Docker smoke**：為了讓第二個 container 可直連，測試用 `0.0.0.0` 僅限 isolated Docker network；不代表實際部署建議

### 注意事項

- `daemon start` 仍不支援 `--endpoint`
- `file.push / file.pull` 在 remote 模式下的 `local_path` 仍是 daemon 所在 host/container 的路徑，不是 client container 的本地路徑

## 研究報告

詳見 `docs/func-test/research-test.md`。
