# serialwrap 架構文件（總覽＋功能頁）

驗收入口：[原生互動式架構總覽](architecture.html)。主畫布是 SVG 元件、具名方向箭頭與四個行程／函式庫／外部邊界；從總覽上方的功能導覽列可以切到 13 個功能頁（[`features/`](features/README.md)），尚未整合的頁面在導覽列上是灰色、不可點。本檔只補充來源、讀圖與重建方式，不代替架構圖。

## 範圍

總覽描述的是原始碼 revision `12eb9a0ebead674a74bca58ffb0cd462074f0347`（main，含 0.5.0-rc.1 之後合入的 issue 282 停機修正與 OD-107），不是某個發行版的位元組：Rust CLI（`crates/sw-cli`）、Rust daemon（`crates/sw-daemon`、`crates/sw-core`）、平台抽象層（`crates/sw-pal`）與協定／設定型別（`crates/sw-proto`）。每個元件、邊界與關係都釘在該 revision 的原始碼行。

不畫成執行期元件的東西：

- repo 內的 Python daemon（`sw_core/`）是凍結的行為基線。release 以 `scripts/rust/mkbundle.sh` 的 `--stage 2` 建置，manifest 的 daemon `implementation` 為 `rust`，`serialwrapd` 直接進 Rust daemon。
- bundle manifest 仍有 `python_cli_delegate` 與 Python runtime 欄位。Rust CLI 的委派集合是空的（`DELEGATED_GROUPS`），這些欄位是凍結 manifest schema 的相容欄位，委派機制只留作緊急回退。`facts.json` 的 `conflicts` 記錄兩邊的來源與裁定。
- installer／更新與回退、systemd／Windows 工作排程、遠端通道、測試基礎建設（OD-107 的長跑觀測容差屬此類）、未合併的分支與計畫。

## 讀圖

中央 1–6 是一次 `line` 命令的實際呼叫鏈：CLI／Agent → RPC／Service → 命令排程 → 命令執行 → UART bridge → UART／DUT。這些數字不是 Cortex WorkflowRun 的 phase，也不代表每個元件會派一個模型。

四個邊界（Archify `region`）都是真實的行程／函式庫／外部邊界，不是閱讀分組：

| 邊界 | 成員 | 依據 |
|---|---|---|
| 使用者端 | CLI／Agent、Human console | CLI 是使用者或 agent 啟動的 `serialwrap` 行程；人工終端程式經 daemon 交出的 PTY slave 或 127.0.0.1 端點接入 |
| serialwrapd（daemon 行程） | Service、命令排程、命令執行、UART bridge、SessionManager、Session actor、TX Fence、ConsoleHub、互動租約、Timeout／Recovery、Command records、BackgroundCapture、file.push／pull、Flash／交接、Event Engine、DeviceWatcher、serialwrapd 主流程、Profiles／契約、state.json、WAL／mirror | `serialwrapd` 執行檔內的元件 |
| 平台層 sw-pal（CLI 與 daemon 共用的函式庫） | 序列埠、RPC transport、console 端點、實例鎖、裝置來源、檔案層／行程掃描 | 平台相依 cfg 只出現在 sw-pal，兩個執行檔都以 `sw_pal::native_client()` 組出 Platform |
| 外部 | UART／DUT、外部 flasher、外部 handler | serialwrap 以外的裝置與程式 |

歸屬有疑義的元件依原始碼判斷，理由寫在各元件的 `responsibility`：

- **state.json、WAL／mirror 畫在 daemon 邊界內**：節點代表 daemon 行程內唯一的寫入者（`StateStore` 執行緒、`WalWriter`）與它們獨占寫入的檔案；檔案 I/O 經平台層的檔案層。
- **Profiles／契約畫在 daemon 邊界內**：節點代表 serialwrapd 啟動時以 `load_profiles` 載入、交給 SessionManager 套用的 profile 集合。CLI 的 `profile test` 也會離線讀同一份 YAML 做 regex 檢查，但不參與執行期，不畫成關係；兩邊的來源與裁定記在 `facts.json` 的 `conflicts`（`profiles-ownership`）。
- **DeviceWatcher 畫在 daemon 邊界內**：程式碼位於 sw-pal（`device_watcher.rs`），但它是 daemon 啟動的執行緒、本身沒有平台差異；平台相依的掃描在平台層的「裝置來源」。

和 Python 版不同的地方照 Rust 原始碼畫：

- **Session 閘門移到 actor**。Service 只取 admission ticket、轉型參數、解析 selector；READY／quiet 等判定在 `SessionCore::submit`。每個 session 一條 actor 執行緒，`SessionCore` 擁有 Session 狀態與世代，只輸出 Effect；開埠、登入、兩階段 drain、持久化由 SessionManager 的執行器去做。
- **命令排程沒有自己的 worker**。`CommandRegistry` 記錄命令，`SessionQueue` 排序；出列在 actor 執行緒上進行，再把 `StartOp` 交給 OpRunner。OpRunner 是隨 bridge 建立的執行緒，只做 I/O，不是獨立服務。
- **UART bridge 是 IoWorker**：一條 reader 執行緒加唯一的 TxWriter 執行緒。每筆 TX 送出前由 TX Fence 判定，拒絕就是零 TX。
- **file.push／pull 佔用前景槽**，由 OpRunner 前景 lane 執行，不進命令佇列，也不繞過 OpRunner 直接寫 bridge。
- **ConsoleHub** 是每個 session 一條的執行緒，負責 RX fan-out 與 console 輸入；誰能寫 UART 仍由 SessionCore 的互動仲裁決定。

命令紀錄的 `accepted → running → done／error／canceled／interactive` 不等於 Session 狀態；`done` 不代表 background 工作完成，也不代表取得了目標命令的 exit code。Human 與事件 handler 不是 Session 狀態 writer。支線：flash 的 `FLASHING` 結束後恢復原 Session；device release 進 `RELEASED`、關閉 handle、等顯式 attach。Timeout recovery 依序送 Ctrl-C／Ctrl-D，恢復就清掉佇列、否則降階，不重送命令。

**停機（issue 282）**：stopping gate 時各 session 的 TX Fence 凍結；若有 file.push／pull 的 echo-paced 命令列正在送，該 Fence 先進入「待凍結」，只放行那一行的 FileTransfer TX。那一行若一個字元都還沒送就直接取消；已送出部分則在 750 ms 內續送並等 echo，完成才補換行，逾時或 echo 停滯就送 Ctrl-U＋換行清掉半行。manager 最多等 1250 ms 後強制凍結，等待耗時從 Phase 1～2 共用的 5 s drain 預算扣除。

## 平台差異

圖上「A｜B」一律表示 Linux｜Windows，**只寫在平台層六個節點的 sublabel**；其他元件的 sublabel 不再帶平台字樣。

| 平台層節點 | Linux | Windows | 使用者（圖上的箭頭） |
|---|---|---|---|
| 序列埠 | termios（開埠前以 /proc 做 holder 診斷） | overlapped COM（holder_unknown） | UART bridge（SessionManager 執行器開埠後交給 IoWorker） |
| RPC transport | AF_UNIX（預設）／TCP loopback | TCP loopback | CLI connect；serialwrapd 主流程 bind，RpcServer 在其上 accept |
| console 端點 | PTY | 127.0.0.1 TCP＋Telnet（另可選 com0com） | SessionManager 建 PTY；ConsoleHub 以它建立 console 多工 |
| 實例鎖 | flock | LockFileEx | serialwrapd 主流程 |
| 裝置來源 | `/dev/serial/by-id`、by-path | SERIALCOMM registry（排除藍牙與指定 COM） | DeviceWatcher |
| 檔案層／行程掃描 | rename＋目錄 fsync；/proc | 取代並重試；Toolhelp32 | state.json、WAL；CLI 的 daemon start／status、doctor 以行程掃描偵測多開 |

flash 的 `/dev/ttyMCU` 端點只在 Linux；Windows 由外部工具走 device release／attach。Windows 一側只依原始碼的 cfg 分支判讀，本次沒有在 Windows 執行；`facts.json` 的 `unknowns` 另列未對實體 DUT 驗收、未核對 release 資產的 manifest 兩項。

## 與前一版（85a16533）的差異

- 來源 revision 改釘 `12eb9a0e`，每條 evidence 重新核對：185 條內容與行號不變、19 條內容不變只移了行號、6 條因 issue 282 內容改變而重寫（`FenceState`、`Fence::admit`、`FenceSubmitGate`、停止序步驟 3～5）。
- 新增四個邊界（前一版沒有邊界）。
- `pal`（sw-pal 平台後端）依原始碼拆成 `pal-serial`、`pal-rpc`、`pal-console`、`pal-lock`、`pal-devices`、`pal-files` 六個節點（平台能力的身分改變，依「Preserve stable IDs」改用新 id，舊 id 不再出現）；原本的 `pal-to-daemon-main`（native() 後端）由九條「使用者 → 平台能力」的關係取代：`bridge-to-pal-serial`、`cli-to-pal-rpc`、`cli-to-pal-files`、`daemon-main-to-pal-rpc`、`sessions-to-pal-console`、`daemon-main-to-pal-lock`、`watcher-to-pal-devices`、`state-store-to-pal-files`、`wal-to-pal-files`。RpcServer 在 listener 上 accept、ConsoleHub 以 console 能力建立多工也是事實，寫在平台層節點的 responsibility；圖上以實際 bind listener 的 serialwrapd 主流程、實際建 PTY 的 SessionManager 為箭頭起點（加這兩條會讓線穿過其他節點，v3.2.0 無法排出）。`flasher-to-handoff` 的標籤由「sync bytes｜release」改為「sync bytes／release」（「｜」只用於 Linux｜Windows）。
- 其他元件 id 都保留；`service`、`bridge`、`console-hub`、`daemon-main`、`watcher`、`human`、`flasher` 的 sublabel 拿掉平台字樣；`profiles` 的 type 由 external 改為 database（daemon 載入的設定資料），並歸入 daemon 邊界。新增 conflict `profiles-ownership` 記錄 daemon 載入與 CLI 離線 `profile test` 兩邊的來源與裁定。
- `meta.title` 不再寫版本號，改為描述性標題「serialwrap 架構總覽｜命令、Session 與 UART」。

## 讀圖時已知的限制

- 加上邊界與平台層後，跨邊界的長線變多。Archify 的交付回條列出 16 處交叉（前一版是 3 處加 1 處回條不計的），全部由 Archify 以 crossover 標記解開（`properCrossings` 為 0），並建議人工看圖，不算錯誤：命令排程附近 4 處（`arbiter→lease` 與 `execute→recovery`、`file-transfer→execute`；`daemon-main→service` 與 `arbiter→command-records`、`command-records→service`）；Session 控制與平台層之間 6 處（`daemon-main`、`sessions`、`watcher`、`state.json` 的折線與往平台層的線相交）；UART bridge 周圍 4 處（`handoff→session-actor` 與 `bridge→pal-serial`、`bridge→wal`；`fence→bridge` 與 bridge／Session actor 的往返線）；人工終端與 ConsoleHub 的線和 file.push／pull 的兩條線 2 處。命令紀錄類節點（Command records 以外的 BackgroundCapture、Timeout／Recovery、file.push／pull）放在主線上方，Session 控制放在主線下方、緊鄰平台層；人工終端與 ConsoleHub 之間的兩條線從使用者端繞過 daemon 邊界上方，CLI 往平台層的兩條線繞過平台層下方。
- 圖比前一版大（31 個節點、48 條關係、4 個邊界），visual-check 量到的最小投影字級：1440×900 為 6.55 px（前一版 7.52 px）、1600×1000 為 7.33 px、1920×1080 與 2048×1320 為 8 px；Archify 的 desktop-readability 檢查通過。1440 寬時可以把指標移到圖上用滾輪放大。
- 圖下方的「節點索引」是 Archify 3.x Reader 的固定側欄，排在圖之後；第一屏是完整的圖，往下捲才是索引。

## 語言

架構標籤是繁體中文，技術名詞與識別字保留原文。Archify 3.2.0 內建 `zh-TW` 目錄，`meta.locale` 設為 `zh-TW`，Viewer 的固定介面也是繁體中文（上游註明該目錄由簡體轉換後人工校對，未經母語者獨立審閱）。頁面沒有網路 runtime 相依；GitHub blob 頁只顯示來源，互動驗收要以瀏覽器開啟下載的 HTML。不部署 github.io。

## HTML 加工

每一頁的 HTML 都是「原版 Archify 交付＋`tools/architecture_overlay.py` 加工」。加工只有兩種，規則見 [`features/README.md`](features/README.md)「HTML 加工規則」與 architecture-fact-layer skill 的「Viewer overlay」：

- **滾輪縮放**：主 SVG 沒有超過視窗高度時（總覽），指標在圖上滾輪縮放，以游標為中心（`Archify.view.centerAt`）；超過時（功能頁的直向長頁，或總覽在窄螢幕變高）滾輪照常捲動、`Alt`＋滾輪縮放。指標不在圖上照常捲動頁面，`Ctrl`＋滾輪留給瀏覽器。
- **功能導覽列**：列「架構總覽」與 13 個功能頁（續頁是主頁後面的子項目），標出目前頁；尚未整合的頁面是不可點的灰色項目。頁面清單的單一來源是 [`pages.json`](pages.json)，可點的頁面就是 `toolchain.json` 記錄的頁面。

`tools/architecture_overlay.py` 與 custom-skills `architecture-fact-layer/scripts/viewer_overlay.py` 逐位元組相同，`toolchain.json` 的 `overlay` 記錄其 sha256 與 skill commit。

## 整合功能頁

功能頁分支（`feature/arch-feature-<NN>-<slug>`）交付 facts、IR 與**原版** Archify HTML 三個檔。收到後：

1. **合入三個檔**：把分支 merge 進 `feature/architecture-features`（只會新增 `docs/architecture/features/<NN>-<slug>.*`）。續頁要先列進 `pages.json` 該功能的 `continuations`。
2. **整合該頁**：`python3 tools/architecture_integrate.py <NN>-<slug> --archify-root <archify>/archify`。它依序：以 `tools/architecture_feature_check.py` 驗事實與投影（0 錯誤才繼續）；以釘選的 Archify 重新交付 IR，確認提交的 HTML 是原版輸出（逐位元組相同，沒有被手改）；套用加工；在 `toolchain.json` 記下該頁的 facts／IR／原版交付／最終 HTML sha256 與加工版本。這一步只寫該頁的 HTML 與 `toolchain.json` 的該頁項目，不碰別頁。
3. **更新導覽列**：所有要整合的頁都做完第 2 步後，執行 `python3 tools/architecture_integrate.py --refresh-nav`，依目前已整合的頁面重新產生每一頁（含總覽）的加工區塊，讓導覽列上新整合的頁面變成可點的連結。只替換加工區塊，原版交付不變（工具會先核對移除加工後的雜湊）。
4. **驗證**：跑下一節的 review、browser、visual-check 與 pytest。

## 來源及重建

`toolchain.json`（schema 2）固定 architecture-fact-layer skill 的 revision、原生 Archify 的版本與 commit（v3.2.0）、加工工具的 sha256 與版本，並為每一頁（`overview` 與已整合的功能頁）記錄來源 revision、圖型、facts／IR／原版交付／最終 HTML 的 SHA-256。`facts.json` 是來源證據與語意權威；`architecture.json` 是它的投影加上幾何；HTML 是原版 Archify 的輸出加上加工，不手改。

Archify 3.x 的 `deliver`／`finalize` 會向上游查更新。重建與 CI 一律設 `ARCHIFY_UPDATE_CHECK_DISABLED=1` 關閉；`tools/architecture_review.py` 自己會帶這個環境變數，並檢查回條確實是關閉狀態。

在有該 revision 的完整 serialwrap checkout，以及 checkout 到釘選 commit 的 Archify 下：

```bash
export ARCHIFY_UPDATE_CHECK_DISABLED=1
python3 -m pytest -q tests/test_architecture_docs.py
python3 tools/architecture_review.py --archify-root /path/to/archify/archify
python3 tools/architecture_browser.py --all --output /tmp/serialwrap-architecture-review
node /path/to/archify/archify/bin/archify.mjs visual-check docs/architecture/architecture.html --json --out-dir /tmp/serialwrap-architecture-review/visual-check
```

`tools/architecture_review.py` 核對每一頁的雜湊、加工區塊與事實，並以同一份 IR 重新交付、套用加工後逐位元組比對。`tools/architecture_browser.py` 以 `file://` 開啟每一頁，核對節點與方向箭頭和 IR 一致；總覽在 1440×900、1600×1000、1920×1080、2048×1320 檢查沒有水平溢出、整張圖落在第一屏（Archify 3.x 的節點索引側欄不計入，與上游 `visual-check` 相同），功能頁檢查第一屏看得到標題、全部參與者表頭與第一個 phase；並驗滾輪縮放（或長頁的捲動與 `Alt`＋滾輪）、導覽列每個連結的目標檔存在、灰色項目不可點，最後實際點一次導覽列連結。

來源／投影 gate、原生 9/9 delivery、逐位元組重建、file 瀏覽器驗證、看過像素、使用者驗收、實際 DUT 端到端測試是不同的宣稱。此文件不操作實際 UART，不以文件測試宣稱實機流程成功。永久 CI 僅具 `contents: read`。
