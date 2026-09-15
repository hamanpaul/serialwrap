# serialwrap 流程架構

驗收入口：[原生互動式架構 HTML](architecture.html)。主畫布是 SVG 元件及具名方向箭頭；本檔只補充來源與重建方式，不代替架構圖。

## 讀圖

中央 1–6 表示一次 `line` 命令的實際呼叫鏈：CLI／Agent → RPC／Service admission → CommandArbiter 佇列及 worker → SessionManager.execute_command → UARTBridge → 實際 UART／DUT。這些數字不是 Cortex WorkflowRun 的 phase，也不意味每個元件會派一個模型。`execute_command` 是 SessionManager 內部函式，不是另一個服務。

上方是 Session 控制：Profiles 是設定契約；DeviceWatcher 觀測裝置；SessionManager 擁有 Session 狀態及 bridge 生命週期；`state.json` 只持久化來源明列的 aliases、bindings、released、profile_pins、profile_detected，不是命令歷程資料庫。Session 的 `READY` 是命令 admission 必要條件，但仍受 readiness reconfirm、boot quiet、recovery、interactive 及 flash gate 限制。

下方分開呈現命令紀錄、background capture、interactive lease、human console、WAL 與事件引擎。命令紀錄的 `accepted → running → done/error/canceled/interactive` 不等於 Session 狀態；`done` 也不代表 background 工作完成，或任意目標 shell 命令的 exit code 已被可靠取得。`result.tail`／WAL 另有查詢與證據語意。

支線明示 `file.push/pull` 直用 bridge；flash 的 `FLASHING` 結束恢復原 Session，而 device release 進 `RELEASED`、釋出 FD 並等待顯式 attach。Timeout recovery 依既有條件清列或降階，不畫成盲目重送整條命令。Human 與事件 handler 不是 Session 狀態 writer。

架構標籤使用繁體中文；固定 Viewer UI 依 upstream 支援回退英文。頁面無網路 runtime 相依；GitHub blob 頁只顯示來源，互動驗收需以瀏覽器開啟下載的 HTML。不部署 github.io。

## 來源及重建

`toolchain.json` 固定來源 revision、使用的已合併 architecture-fact-layer skill 與原生 Archify revision，並綁定三份交付檔案的 SHA-256。`facts.json` 是來源證據與語意權威，JSON 及 HTML 不得自行新增流程。未合併的 refine 計畫不提升為既有實作。

在有該 revision 的完整 serialwrap checkout，以及固定版本的 Archify checkout 下：

```bash
python3 -m pytest -q tests/test_architecture_docs.py
python3 tools/architecture_review.py --archify-root /path/to/archify/archify
python3 tools/architecture_browser.py --html docs/architecture/architecture.html --output /tmp/serialwrap-architecture-review
node /path/to/archify/archify/bin/archify.mjs visual-check docs/architecture/architecture.html --json
```

來源／投影 gate、原生 9/9 delivery、逐位元組重建、file 瀏覽器驗證、視覺檢查、使用者驗收、實際 DUT 端到端測試是不同宣稱。此 PR 不操作實際 UART，不以文件測試宣稱實機流程成功。永久 CI 僅具 contents:read，未包含一次性匯出或傳輸流程。
