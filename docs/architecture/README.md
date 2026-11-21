# serialwrap 流程架構（0.5.0，Stage 2：Rust CLI＋Rust daemon）

驗收入口：[原生互動式架構 HTML](architecture.html)。主畫布是 SVG 元件及具名方向箭頭；本檔只補充來源與重建方式，不代替架構圖。

## 範圍

圖描述 0.5.0 實際出貨的執行期：Rust CLI（`crates/sw-cli`）、Rust daemon（`crates/sw-daemon`、`crates/sw-core`）、平台抽象層（`crates/sw-pal`）與協定／設定型別（`crates/sw-proto`）。來源 revision 是 Stage 2 合併點 `85a16533`；每個元件與關係都釘在該 revision 的原始碼行。

不畫成執行期元件的東西：

- repo 內的 Python daemon（`sw_core/`）是凍結的行為基線。release 以 `scripts/rust/mkbundle.sh` 的 `--stage 2` 建置，manifest 的 daemon `implementation` 為 `rust`，`serialwrapd` 直接進 Rust daemon。
- bundle manifest 仍有 `python_cli_delegate` 與 Python runtime 欄位。Rust CLI 的委派集合是空的（`DELEGATED_GROUPS`），沒有子命令委派給 Python；這些欄位是凍結 manifest schema 的相容欄位，委派機制只留作緊急回退。`facts.json` 的 `conflicts` 記錄兩邊的來源與裁定。
- installer／更新與回退、systemd／Windows 工作排程、遠端通道、測試基礎建設、未合併的分支與計畫。

## 讀圖

中央 1–6 是一次 `line` 命令的實際呼叫鏈：CLI／Agent → RPC／Service → 命令排程 → 命令執行 → UART bridge → UART／DUT。這些數字不是 Cortex WorkflowRun 的 phase，也不代表每個元件會派一個模型。

和 Python 版不同的地方照 Rust 原始碼畫：

- **Session 閘門移到 actor**。Service 只取 admission ticket、轉型參數、解析 selector；READY／quiet 等判定在 `SessionCore::submit`。每個 session 一條 actor 執行緒，`SessionCore` 擁有 Session 狀態與世代，只輸出 Effect；開埠、登入、兩階段 drain、持久化由 SessionManager 的執行器去做。
- **命令排程沒有自己的 worker**。`CommandRegistry` 記錄命令，`SessionQueue` 排序；出列在 actor 執行緒上進行，再把 `StartOp` 交給 OpRunner。OpRunner 是隨 bridge 建立的執行緒，只做 I/O，不是獨立服務。
- **UART bridge 是 IoWorker**：一條 reader 執行緒加唯一的 TxWriter 執行緒。每筆 TX 送出前由 TX Fence 判定，拒絕就是零 TX。
- **file.push／pull 佔用前景槽**，由 OpRunner 前景 lane 執行，不進命令佇列，也不繞過 OpRunner 直接寫 bridge。
- **ConsoleHub** 是每個 session 一條的執行緒，負責 RX fan-out 與 console 輸入；誰能寫 UART 仍由 SessionCore 的互動仲裁決定。

上方是 Session 控制：Profiles 是設定契約；DeviceWatcher 觀測裝置；SessionManager 管裝置快照、COM rank 與路由；`state.json` 只持久化 aliases、bindings、released、profile_pins、profile_detected 五個 key。左上的 `serialwrapd 主流程` 是啟動與停止序的 owner；`sw-pal 平台後端` 是它注入的平台能力包。

下方分開呈現命令紀錄、background capture、互動租約、human console、WAL 與事件引擎。命令紀錄的 `accepted → running → done／error／canceled／interactive` 不等於 Session 狀態；`done` 不代表 background 工作完成，也不代表取得了目標命令的 exit code。Human 與事件 handler 不是 Session 狀態 writer。

支線：flash 的 `FLASHING` 結束後恢復原 Session；device release 進 `RELEASED`、關閉 handle、等顯式 attach。Timeout recovery 依序送 Ctrl-C／Ctrl-D，恢復就清掉佇列、否則降階，不重送命令。

## 平台差異

圖上「A｜B」一律表示 Linux｜Windows，寫在元件的 sublabel。這是後端差異的標示，不是部署或安全邊界；圖上沒有 boundary。

| 接縫 | Linux | Windows |
|---|---|---|
| RPC | AF_UNIX（預設），也支援 TCP loopback | TCP loopback |
| 序列埠 | termios | overlapped COM |
| human console | PTY | 127.0.0.1 TCP＋Telnet（另可選 com0com） |
| 實例鎖 | flock | LockFileEx |
| 裝置來源 | `/dev/serial/by-id`、by-path | SERIALCOMM registry |
| flash 端點 | `/dev/ttyMCU` | 無；外部工具走 device release／attach |

Windows 一側只依原始碼的 cfg 分支判讀，本次沒有在 Windows 執行；`facts.json` 的 `unknowns` 另列未對實體 DUT 驗收、未核對 release 資產的 manifest 兩項。

## 讀圖時已知的限制

- 命令排程下方有線條交叉。Archify 的交付回條列出三處（`arbiter→lease` 與 `file-transfer→execute`、`execute→recovery` 與 `file-transfer→execute`、`capture→service` 與 `command-records→service`），並建議人工看圖，不算錯誤；另有一處是同樣接在命令排程底邊的 `arbiter→lease` 與 `recovery→arbiter` 相交，回條不計。四處都是垂直相交，方向與端點仍可辨識。
- 1440×900 時關係標籤約 7.5px，是 Archify 對桌面閱讀設定的目標值；較大的視窗會放大。
- 圖下方的「節點索引」是 Archify 3.x Reader 的固定側欄，排在圖之後；第一屏是完整的圖，往下捲才是索引。

## 語言

架構標籤是繁體中文，技術名詞與識別字保留原文。Archify 3.2.0 內建 `zh-TW` 目錄，`meta.locale` 設為 `zh-TW`，Viewer 的固定介面也是繁體中文（上游註明該目錄由簡體轉換後人工校對，未經母語者獨立審閱）。頁面沒有網路 runtime 相依；GitHub blob 頁只顯示來源，互動驗收要以瀏覽器開啟下載的 HTML。不部署 github.io。

## 來源及重建

`toolchain.json` 固定來源 revision、architecture-fact-layer skill 的 revision、原生 Archify 的版本與 commit（v3.2.0），並綁定三份交付檔案的 SHA-256。`facts.json` 是來源證據與語意權威；`architecture.json` 是它的投影加上幾何，`architecture.html` 是原版 Archify 的輸出，不手改。

Archify 3.x 的 `deliver`／`finalize` 會向上游查更新。重建與 CI 一律設 `ARCHIFY_UPDATE_CHECK_DISABLED=1` 關閉；`tools/architecture_review.py` 自己會帶這個環境變數，並檢查回條確實是關閉狀態。

在有該 revision 的完整 serialwrap checkout，以及 checkout 到釘選 commit 的 Archify 下：

```bash
export ARCHIFY_UPDATE_CHECK_DISABLED=1
python3 -m pytest -q tests/test_architecture_docs.py
python3 tools/architecture_review.py --archify-root /path/to/archify/archify
python3 tools/architecture_browser.py --html docs/architecture/architecture.html --output /tmp/serialwrap-architecture-review
node /path/to/archify/archify/bin/archify.mjs visual-check docs/architecture/architecture.html --json --out-dir /tmp/serialwrap-architecture-review/visual-check
```

`tools/architecture_review.py` 以同一份 IR 重新交付並逐位元組比對。`tools/architecture_browser.py` 以 `file://` 開啟頁面，核對節點與方向箭頭和 IR 一致，並在 1440×900、1600×1000、1920×1080、2048×1320 檢查沒有水平溢出、整張圖落在第一屏。Archify 3.x 的節點索引側欄不計入一屏判定，這和上游 `visual-check` 的定義相同。

來源／投影 gate、原生 9/9 delivery、逐位元組重建、file 瀏覽器驗證、看過像素、使用者驗收、實際 DUT 端到端測試是不同的宣稱。此文件不操作實際 UART，不以文件測試宣稱實機流程成功。永久 CI 僅具 `contents: read`。
