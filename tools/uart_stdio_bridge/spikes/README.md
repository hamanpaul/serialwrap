# socat 接線 Spike（§15.4.1 第一步）

本目錄包含 `uart_stdio_bridge` 在實作正式 Python bridge（Task 2～5）之前的先導 spike 腳本與驗證成果。

## 1. 用途與定位

依據重構規格書 §15.4.1：
> **第一步：socat 接線 Spike。**  
> 可用 `socat` 的 PTY＋EXEC 功能確認原始資料轉接、TTY raw 設定、EOF 與子程序連接。這只是拓樸可行性檢查，不作為正式差分證據格式。

本 spike 用以驗證：
1. **二進位保真（raw roundtrip）**：經由 PTY 與子程序管道雙向轉接 256 位元組全值域（`bytes(range(256)) * 4` = 1024 bytes），無任何字元截斷、遺漏或轉譯。
2. **無本地回顯（no-echo）**：PTY 端配置 `raw,echo=0`，確保終端機層級無本地 echo 污染 UART 串流。
3. **子程序結束時之 EOF 語意（EOF on child exit）**：當受測子程序 exit 0 結束後，PTY 讀端能否在合理時間內感知 EOF（0-byte read）或 EIO（Input/output error）。
4. **EOF 延遲量測（EOF latency）**：量測從寫入 `__EXIT__\n` 到 PTY 感知關閉（收到 EOF/EIO）的時間差（`eof_latency_ms`）。特別注意：此值主要為 `socat` 預設 `-t 0.5` 的 linger 等待時間（約 500 ms；若改以 `-t 0.1` 測試則約 104 ms），並非 PTY 或子程序 pipe 的固有延遲，不得作為正式 bridge 轉接的延遲預算上限。Task 5 README 引用時亦須帶同一說明。

## 2. 邊界與限制

- **非正式差分證據**：本檢查屬拓樸可行性驗證，不產出 `comparison.v1` 或 `bridge.v1` 差分報告。
- **嚴格無 verdict**：本報告絕不包含 `verdict` 欄位，不得用作發版或驗收之 gate 證據（§15.4.1、計畫 Task 9/T9-1）。
- **非必要 gate**：依賴 `socat`（1.8+）；環境缺少 `socat` 時輸出 `{"result":"NOT_RUN","reason":"socat_missing"}` 並以 exit 0 結束，不阻擋非 POSIX 或未安裝 socat 之環境。但 exit 0 僅代表環境略過執行，不代表 gate 通過。依據計畫 I9-2，正式進入 Task 2～5 前，本機環境仍須具備 socat 且 `raw_roundtrip`、`no_echo`、`eof_on_child_exit` 三項皆為 `true`，任一為 `false` 或 `NOT_RUN` 即須停止並回報。
- **僅限 POSIX 拓樸**：本 spike 僅驗證 Linux/WSL2 之 POSIX PTY＋EXEC 拓樸可行性，並非 Windows COM 串接證據（§11.2、§15.8.3）。Windows 平台的 COM 接線驗證由 Task 10 及後續自測負責。
- **環境變數與路徑限制**：支援以 `PYTHON` 環境變數自訂 Python 直譯器（預設 `python3`）。`socat` 之 `EXEC:...` 參數若包含空白或逗號，可能被 socat 語法拆解錯誤，執行路徑應避免特殊字元。
- **獨立性守衛**：所有腳本僅使用 Python 標準函式庫，嚴格禁止 import `sw_core` 或產品內部實作。

## 3. 檔案與工具清單

| 檔案 | 語言 / 形式 | 用途說明 |
|---|---|---|
| `socat_wiring.sh` | Bash (POSIX) | 拓樸接線與測試協調腳本，支援 `--out <DIR>` 輸出報告 |
| `echo_child.py` | Python 3 (stdlib) | 二進位原樣回寫之 echo 子行程，讀到位元組序列 `__EXIT__\n` / `__EXIT__\r\n` 時 exit 0 |
| `README.md` | Markdown | 本說明文件 |

## 4. 執行方式

```bash
# 執行 spike 並將報告儲存至指定的輸出目錄
bash tools/uart_stdio_bridge/spikes/socat_wiring.sh --out ~/b-log/rust-refactor/p2/socat-spike
```

### 相關測試

```bash
python3 -m pytest -q tools/uart_stdio_bridge/tests/test_socat_spike.py
```

## 5. 輸出報告規格（`spike-report.json`）

報告輸出至 `$OUT/spike-report.json`，格式如下：

```json
{
  "checks": {
    "eof_latency_ms": 504,
    "eof_on_child_exit": true,
    "no_echo": true,
    "raw_roundtrip": true
  },
  "note": "拓樸可行性檢查，非差分證據格式（§15.4.1）",
  "schema_version": "socat-spike.v1",
  "socat_version": "1.8.0.0"
}
```

### 欄位說明

- `schema_version` (string): 固定為 `"socat-spike.v1"`。
- `socat_version` (string): 執行環境之 `socat` 版本字串（例如 `"1.8.0.0"`）。
- `checks.raw_roundtrip` (bool): 寫入 1024 位元組全值域二進位資料並完整無誤讀回為 `true`。
- `checks.no_echo` (bool): 確認無終端機本地回顯且讀端無多餘殘留位元組為 `true`。
- `checks.eof_on_child_exit` (bool): 子程序結束後 PTY 讀端收到 EOF/EIO 為 `true`。
- `checks.eof_latency_ms` (int): 寫入 `__EXIT__\n` 至收到 EOF/EIO 的時間差（毫秒；主要反映 `socat` 預設 `-t 0.5` 之等待行為，非 PTY 固有延遲）。
- `note` (string): 固定標註文字「拓樸可行性檢查，非差分證據格式（§15.4.1）」。

此外，`$OUT/socat.stderr.log` 會完整保存 `socat -d -d` 之行程啟動、PTY 建立與子行程生命週期診斷輸出。

## 6. 實測成果與前置 Gate 判定

於本機環境（Ubuntu WSL2、socat 1.8.0.0）實測結果：
- `raw_roundtrip`: `true`
- `no_echo`: `true`
- `eof_on_child_exit`: `true`
- `eof_latency_ms`: 約 504 ms

三項拓樸檢查皆為 `true`，滿足 Task 2～5 正式 bridge 實作的前置 gate 條件。實測之 `eof_latency_ms`（約 504 ms）主要源自 `socat` 預設 `-t 0.5` 之 linger 等待（改 `-t 0.1` 則約 104 ms），正式 bridge（Task 2～5）直接透過 Popen pipe EOF 與 `wait()` 管理子程序生命週期，延遲為毫秒級，不得以 500 ms 作為內部橋接延遲預算。
