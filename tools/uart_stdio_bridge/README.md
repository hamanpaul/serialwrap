# uart_stdio_bridge — 外部 UART-to-stdio Bridge 與測試基礎工具

`uart_stdio_bridge` 為 `serialwrap` 重構 Stage 0 建立的獨立外部測試工具發行單元，提供 L1.5 / L2 / L3 測試所需的外部 byte-stream 觀測、DUT 模擬器介接、WAL↔wire 雙向比對及故障注入通道。

## 1. 測試拓樸與架構

依據規格書 §15.3，`serialwrap` 與 `bridge` 執行於待驗證的原生作業系統，DUT 模擬器可透過 Docker `-i` 容器或本地子行程（`local-process`）連接：

```text
       ┌───────────────────────────────┐
       │   serialwrap (Python / Rust)  │
       └──────────────┬────────────────┘
                      │ 端點 A (PTY slave / COM-A)
                      ▼
              虛擬序列埠 pair (POSIX PTY master / com0com)
                      ▲
                      │ 端點 B (PTY master / COM-B)
       ┌──────────────┴────────────────┐
       │       uart_stdio_bridge       │◄─── 獨立控制通道 (TCP loopback 127.0.0.1)
       └──────────────┬────────────────┘
                      │ stdin / stdout (無 TTY 封裝)
                      ▼
       ┌───────────────────────────────┐
       │   DUT 模擬器 (Docker / 本地)   │
       └───────────────────────────────┘
```

### 關鍵原則：bridge 只持有端點 B
- `bridge` 僅持有虛擬埠對端 B（POSIX: PTY master；Windows: COM-B），絕不持有或開啟受測端 `serialwrap` 的端點 A。
- `bridge` 不與 daemon 競爭同一序列埠端點。

### POSIX PTY 的世代邊界
- PTY master 已接受、但 peer 尚未讀取的 bytes，可能在 peer 關閉後留給下一個 opener。需要隔離情境上下文時，先 `reconnect()` 建立新端點世代，再啟動下一個 opener。
- `disconnect(remove_link=False)` 會保留指向已釋放 `/dev/pts/N` 的 symlink；核心可能把該編號重新指派給其他行程。此選項只可在隔離的測試目錄使用。
- `peer_open()` 回報呼叫當下的狀態，不是事件歷史。每 50 ms 的 peer watcher 可能漏掉在兩次取樣之間完成的關閉與重開；需要事件級證據時，不得只依賴此狀態欄位。

## 2. 資料流方向定義

方向一律以 `serialwrap` 視角定義：

- **TX**：`serialwrap` → 虛擬序列埠 → `bridge` → DUT `stdin`
- **RX**：`serialwrap` ← 虛擬序列埠 ← `bridge` ← DUT `stdout`

四個核心觀測點（§15.6.2）：
- `serial_read` (TX)：bridge 已從虛擬埠對端讀到 serialwrap 送出的 bytes。
- `dut_stdin_write` (TX)：bridge 對 DUT 程序 stdin 寫入已完成。
- `dut_stdout_read` (RX)：bridge 已從 DUT 程序 stdout 讀到 bytes。
- `serial_write` (RX)：bridge 對虛擬埠對端的寫入已完成。

## 3. 獨立性與邊界守衛（OD-07、§15.4.2）

- **完全獨立的發行單元**：擁有自己的 `pyproject.toml`、測試套件與 CI job。
- **嚴格禁止 import 產品程式碼**：`uart_stdio_bridge` 禁止 import `sw_core`、`serialwrapd`、`serialwrap`、`realhw` 或 `serialwrap_regression`，禁止共用產品內部之 Serial、WAL、仲裁、登入 FSM 或 prompt 解析演算法。
- **依賴極簡**：執行期僅依賴 Python 標準函式庫（Windows 端使用 `pyserial==3.5` 作為 COM 埠介面）。

## 4. 測試原因碼（`HARNESS_*`）

- 所有測試工具產生的錯誤與原因碼（定義於 `harness_codes.py`）均以 `HARNESS_` 開頭。
- `HARNESS_*` 屬測試工具契約，**絕不得**混入產品錯誤碼目錄（`contracts/error_codes.yaml`）或 session last error。

## 5. 證據產出檔案（§15.6.1）

每次 L1.5 執行產出之標準證據檔：

| 檔案 | 格式 / Schema | 用途說明 |
|---|---|---|
| `manifest.json` | `l15-manifest.v1` | 記錄受測 artifact、工具身分、情境、平台與端點資訊 |
| `wire.ndjson` | `bridge.v1` | bridge 雙向四個觀測點實際觀測／完成之 byte 事件記錄 |
| `control.ndjson` | `control.v1` | 故障控制、barrier、DUT 重啟及動作完成回條記錄 |
| `dut.stderr.log` | 純文字日誌 | DUT 模擬器與 Docker 之診斷輸出（絕不混入 UART） |
| `correlation.ndjson` | `correlation.v1` | 測試步驟、CLI 請求／命令 ID、byte 區間與 WAL 對應 |
| `comparison.json` | `comparison.v1` | WAL↔wire 比對結果、第一個差異位置或證據不足原因 |

## 6. CLI 基本用法

```bash
# 顯示工具身分與 schema 版本（JSON 輸出）
uart-stdio-bridge version

# 執行 DUT 模擬器（Task 4 實作，轉呼叫 uart_stdio_bridge.dut）
uart-stdio-bridge dut --mode echo [--banner TEXT]          # --banner：啟動即輸出（測 peer gate）
uart-stdio-bridge dut --mode model [--noise-interval-ms N] # 省略 --target-json 時用預設 TargetConfig

# 執行 bridge 轉接與錄證
uart-stdio-bridge run --run-id <run-id> --case-id <case-id> --port-id COM0 \
  --evidence-dir <dir> --endpoint posix-pty --by-id-link <link-path> \
  --dut-mode local-process --dut-argv-json '["python3", "dut.py"]'

# 執行 WAL 與 wire 證據比對（Task 7 實作）
uart-stdio-bridge compare --evidence-dir <dir> --wal-dir <wal-dir> \
  --port-map COM0=COM0 --out comparison.json

# 執行工具資格自測（Task 8 實作）
uart-stdio-bridge selftest --endpoint posix-pty --out <evidence-dir>
```

### `run` 執行與隔離

- `run` 僅開啟序列埠 pair 的 bridge 對端 B；POSIX 以 `--by-id-link` 建立 PTY slave symlink，Windows 以 `--com` 開啟 bridge 端 COM。受測程式自行開啟另一端，bridge 不碰受測端。
- `--dut-mode local-process` 必須帶 `--dut-argv-json` 或 `--dut-target-json`。DUT 的 stdin、stdout、stderr 均以 binary pipe 傳輸；`--dut-mode docker` 使用 `docker run --rm -i`，不配置 TTY。
- `--max-inflight-bytes` 限制 TX、RX 各自的待處理 byte 數。佇列滿時停止對應方向讀取，讓背壓傳回端點；單一 chunk 在空佇列時可大於上限。
- 每次執行使用新的 evidence 目錄。POSIX 目錄與 `control.json` 權限分別限制為 `0700`、`0600`；檔案包含隨機控制權杖，請勿複製到公開位置。
- 啟動後，bridge stdout 只輸出一行 `BRIDGE_READY` JSON，提供 `control_info` 與 PTY slave 路徑或 COM。DUT stdout 不會經過 bridge stdout，而會送往受測端並寫入 `wire.ndjson`。
- DUT stderr 另存為 `dut.stderr.log`。達到 `stderr_cap_bytes` 後停止寫檔但繼續排空 pipe，避免 stderr flood 卡住 UART 資料流；manifest 會標示 `dut_stderr_truncated`。

### 控制通道

控制用 TCP loopback 與 UART bytes 完全分離。每個 JSON line 請求需帶 `action`、`action_id`、`params`、`token`；server 驗證 `control.json` 權杖，錯誤權杖會回 `HARNESS_CONTROL_PROTOCOL_ERROR` 並關閉連線。每個 action 在 `control.ndjson` 記錄 `requested` 與 `completed`／`failed`。

支援的動作包括 `status`、`barrier`（`passive`／`quiesce`）、`checkpoint`、`mark`、`pause_pump`／`resume_pump`、`by_id_link_remove`／`by_id_link_add`、`transport_disconnect`／`transport_reconnect`、`dut_restart`、`dut_kill`、`dut_spawn`、`dut_control` 與 `shutdown`。`quiesce` 會暫停 RX reader 並保留 pipe 內資料，成功後需由呼叫端明確執行 `resume_pump`；逾時時自動恢復。

bridge 證據 writer 失效、磁碟空間不足、端點 I/O 失敗、不確定寫入或未允許的 DUT 結束都會 fail-stop，寫入 `bridge-manifest.json` 並以 exit code `3` 結束。只有實際已接受的寫入長度會記為成功；不確定的端點寫入會留下 `loss_flag: true` 並讓該次驗證失效。

## 7. WAL↔wire 比較器與竄改拒絕（Task 7）

比較器為純離線分析工具，負責比較 `wire.ndjson`（外部 bridge 證據）與 `raw.wal.ndjson`（受測 daemon 記錄）。

### 比對程序（§15.6.3）
1. **完整性先行**：檢驗 `wire.ndjson`、`bridge-manifest.json`、`correlation.ndjson` 與 `raw.wal.ndjson` 之格式、CRC32、seq 連續性、stream_offset 連續性、檔案 SHA-256 及 `loss_flag`。若存在任何竄改或缺口，立即回傳 `INSUFFICIENT_EVIDENCE`。
2. **窗口判定**：以 correlation 中 `settled: true` 快照切定窗口邊界（`wal_seq` 與 `wire_offsets`）。多世代（transport reconnect）必須有切段快照。
3. **串流比對**：TX（WAL TX ↔ wire `serial_read`）與 RX（WAL RX ↔ wire `serial_write`）按位元組連續串接比對，不比 chunk 邊界。
4. **核准映射**：
   - `secret_tx_unlogged`：若差異點位於 WAL 紀錄邊界且 wire TX 內容為指定 secret 加換行，允許略過並記錄 `rules_applied`。
   - `wal_com_alias`：允許一埠對應多個 WAL COM 別名（如 `COM0,PROBE`）。
5. **判定與輸出**：全部相等為 `PASS`（exit 0）；任一不相等為 `FAIL`（exit 1），輸出首個差異偏移量及遮罩後的 32 位元組 hex 上下文；證據不足或竄改為 `INSUFFICIENT_EVIDENCE`（exit 4）。

### `compare` 命令列參數
- `--evidence-dir <DIR>`：bridge 產出證據目錄（必要）。
- `--wal-dir <DIR>`：daemon WAL 產出目錄（必要）。
- `--port-map <PORT=COM0[,COM1...]>`：埠與 WAL COM 映射（必要，可多次指定）。
- `--out <PATH>`：比對報告輸出 JSON 路徑（必要）。
- `--rule <RULE>`：啟用事先核准之比較映射規則（`secret_tx_unlogged`、`wal_com_alias`）。
- `--secret-env <NAME>`：指定含有敏感資訊之環境變數名稱，用於 `secret_tx_unlogged` 規則與差異 hex 遮罩（環境變數未設或為空時回傳 exit 2）。
- `--until-op <OP_ID>`：指定以某操作之 `op_start` settled 快照作為窗口終點；未指定時取最後一個 settled 快照。
- `--checkpoint <PATH>`：bridge 存活期間比對：以 checkpoint 驗證 wire/control 前綴代替 closed manifest。
- `--zero-tx <OP_ID>`：檢驗指定操作期間是否符合零 TX 不變量（可多次指定）。
- `--contiguous-b64 <B64>`：檢驗 Base64 編碼之 payload 在 wire TX 串流中是否連續且有序出現（可多次指定）。

### `redact` 敏感資訊遮罩（§15.6.4）
使用 `--secret-env <NAME>` 指定欲遮罩之環境變數（可多次指定），將 wire 與 WAL payload 中的敏感字串替換為等長 `*`，重算 CRC32/Base64，並產出 `redaction-manifest.json`（標記 `byte_equivalent: false`）。輸出目錄若已存在或環境變數未設/為空則拒絕執行（exit 2）。

## 8. Bridge 工具自測與資格判定（§15.4.3 / Spike 4）

`uart_stdio_bridge` 作為驗證 `serialwrap` 的獨立第三者外部觀測工具，本身具備自我驗證能力。透過自測項目集（ST-01～ST-14）與核准 revision 治理，防止測試工具自身的假陽性或假陰性污染驗證結果。

### 8.1 自測項目總表（ST-01～ST-14）

| 項目代號 | 驗證目標 | 涵蓋範圍與規範 |
|---|---|---|
| `ST-01` | 完整 byte 值域 雙向傳輸驗證 | 0～255 全值域往返、四個觀測串流全等與比較器驗證 |
| `ST-02` | 多區塊 資料傳輸 | 4 MiB 隨機分塊寫入、連續性與 seq/offset 斷裂偵測 |
| `ST-03` | 短讀 ／部分寫入行為 | 精確記錄每筆實際接受長度且串接無重複無遺失（涵蓋延遲讀取與 in-process stub） |
| `ST-04` | stderr 大量輸出隔離與日誌排空 | DUT flood 50 MB 同時 1 MiB UART 往返 |
| `ST-05` | EOF 偵測與世代保持 | stdout 關閉後留下 dut_stdout_eof META 且 TX 仍可轉送 |
| `ST-06` | 程序退出 與非預期離開 fail-stop | DUT 提前 exit 觸發 bridge exit 3 與 HARNESS_DUT_EXITED |
| `ST-07` | 故障控制 通道動作與回條記錄 | 故障控制通道動作完整呼叫、驗證且 control.ndjson 正確配對 |
| `ST-08` | CRC 與 offset 驗證 | 正常結束證據執行 load_wire 與 bridge-manifest digest 檢驗 |
| `ST-09` | 破壞證據 竄改拒絕判定 | 套用 Task 7 的 13 種竄改均拒絕並符合原因碼 |
| `ST-10` | 空間不足 與磁碟滿 fail-stop | RLIMIT_FSIZE / EFBIG / ENOSPC 觸發 HARNESS_DISK_FULL |
| `ST-11` | DUT restart 世代累加與 offset 保持 | 兩次 restart 世代 1→2→3 且 offset 跨世代連續遞增不重設 |
| `ST-12` | transport fault 斷線與重連世代遞增 | peer EIO、世代累加且新世代 offset 從 0 起 |
| `ST-13` | 不確定寫入 fail-stop | 端點 write_some 拋出 EndpointWriteUncertain 留 loss_flag: true |
| `ST-14` | 非預期 overflow 與有界背壓佇列 | writer 阻塞觸發 HARNESS_EVIDENCE_OVERFLOW 與 fail-stop |

### 8.2 核准 Revision 治理政策（`APPROVED_REVISION.json`）

依據 §15.4.3 與 OD-07，所有正式 L1.5 / L2 驗證所使用之 `uart_stdio_bridge` 必須經過獨立審查並鎖定 revision：

```json
{
  "schema_version": "bridge-approved.v1",
  "tree_sha256": "<64-hex-digest>",
  "reviewer": "independent-reviewer",
  "review_ref": "review:task-8",
  "date": "2026-10-17"
}
```

- **雜湊範圍**：`tool_tree_sha256()` 嚴格掃描 `tools/uart_stdio_bridge/src/` 下所有 Python 原始碼，不包含測試、臨時檔與 `APPROVED_REVISION.json` 本身。
- **重新審查觸發**：任何對 `src/` 程式碼的修改均會導致 tree SHA-256 改變。修改者不得私自更新 `APPROVED_REVISION.json`，必須經過獨立審查流程重新簽核。
- **Gate 守衛**：在執行任何以 bridge 為評判依據的測試前，CI gate 會檢查 revision 合格狀態。

### 8.3 資格判定規則（`is_qualified`）

一份合格的自測報告（`report.json`）必須透過 `is_qualified(report_path)` 檢驗並滿足以下嚴格條件：

1. **完整執行**：報告記錄 `complete: true` 且 `not_selected` 為空（表示執行了全套 14 項檢查）。
2. **零失敗與能力適配**：所有 14 個自測項目之判定結果均為 `PASS`（`fails == 0`）；若端點為 `win-serial` 且缺乏 `transport_disconnect` 能力，允許 ST-12 為 `NOT_RUN` 且 reason 標明 `cap_missing:transport_disconnect`。
3. **原始碼樹雜湊一致**：報告中記錄之 `tree_sha256` 必須為有效 64 位元 16 進位字串，且與 `APPROVED_REVISION.json` 中的 `tree_sha256` 完全相符；`APPROVED_REVISION.json` 必須由真實獨立審查者簽署。

### 8.4 CLI `selftest` 執行與退出碼

```bash
# 執行所有自測項目並產出資格報告
uart-stdio-bridge selftest --endpoint posix-pty --out /path/to/evidence_dir

# 檢查結束狀態碼
echo $?
```

退出碼定義：
- `0` (`PASS`)：所有自測項目全數通過（或合規 NOT_RUN），報告產出成功。
- `1` (`FAIL`)：自測項目中有任一項目判定為 `FAIL` 或未預期之 `NOT_RUN`。
- `2` (`ERROR`)：命令列參數不合法或執行期發生未預期異常。



