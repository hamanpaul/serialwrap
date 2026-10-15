# 《serialwrap Rust 跨平台重構架構規格書：定稿版（Final）》

**文件狀態**：定稿版（2026-10-15）。本文經 Gemini 4 Argon 起草、ChatGPT GPT-6 Astra 多輪對抗審查、ChatGPT Pro 整合測試策略，擁有者裁決 OD-01～OD-13，並通過 Astra 定稿前全文審查（27 項）與複核（3 項），全部修正完畢。OD-01～OD-13 已於 2026-10-15 由擁有者裁決（第 14 章），BC-01～BC-05 為 `APPROVED`。核准不等於已實作或已驗收；各項仍須經 `IMPLEMENTED`、`VERIFIED` 才算完成。

**原始分析基準**：v4 所引用的 `main`（分析時為 commit `1637070`）與約 18,000 行 Python `sw_core`。Stage 0 開始時，須記錄確切 Git commit、部署 artifact 與測試工具 revision；單獨記錄分支名稱不足以建立可重現基線。

**驗證架構**：沿用 L1、L1.5、L2、L3 測試金字塔。保留 `func-test/`、`regression/`、`realhw/` 及既有 TestPilot 前端；新增外部 bridge 證據能力，不新增第四套平行測試框架。

**裁決權威**：第 14 章的 Owner Decision Gate 與其裁決紀錄（§14.3）。BC-01～BC-05 已依 OD-04 核准為 `APPROVED`；後續若變更核准範圍，須新增裁決紀錄並重判受影響證據。

## 0. 重構願景、實證依據與四階段推進定位

### 0.1 專案約束與四大核心目標

擁有者主要在 Linux／WSL 開發與自用；主要終端使用者為 Windows 測試團隊。AI Agent、自動化測試與 TestPilot 透過 CLI subprocess、JSON 與 Console 操作已部署的 serialwrap。

本專案維持四項核心目標：

| 目標 | v5 定義 |
|---|---|
| 固定平台層，集中開發 Behavior | 將 Serial I/O、裝置列舉、Console 平台介面、鎖、檔案操作及監管封裝於 `sw-pal`，以明確能力範圍、契約測試與雙平台執行證據控制變更；不宣稱編譯完成後平台問題永久消失 |
| 限制 Agent 越界修改 | 以部署權限、受保護的發佈 artifact、錯誤處置目錄、排障流程及變更審查，禁止 Agent 為完成其他任務而就地修改 serialwrap 或繞過 broker |
| 解決 Windows 使用痛點 | 降低 CLI 啟動與 WAL 熱路徑成本，建立安裝、監管、更新及回滾；保留免驅動 Telnet Console 為必備基準；Serial Console 依 OD-01 為選配 |
| 降低維護與回歸成本 | 沿用既有測試資產，以 L1 驗證邏輯與契約、L1.5 驗證外部可觀測 bytes、L2 防止實機缺陷復發、L3 驗證指定長時間負載下的退化 |

Rust 原生 binary 可以移除 CLI 的 PyInstaller one-file 解壓路徑，但不是權限或安全邊界。若使用者仍可替換 binary、修改其上層目錄或繞過 broker 開啟 UART，僅改用 Rust 不足以達成治理目標。

### 0.2 四階段推進與 Stage 1 生產檢查點

v4 收錄的歷史分析為：51 個跨平台相關 commit 中，扣除 17 個非程式碼 commit，剩餘 34 個程式碼修補；其中 22 個僅涉及 daemon、5 個同時涉及 daemon 與 client、5 個僅涉及 client、2 個涉及其他 `sw_core` 檔案。

依該分析，27／34，約 79.4%，涉及 daemon。此數據支持「僅重寫 CLI 不能涵蓋主要平台維護面」的方向，但不代表 Rust 重寫能消除同等比例的未來維護成本。Stage 0 須保存原分析的 commit 清單及分類依據，供後續校準。

| 階段 | 交付定位 |
|---|---|
| Stage 0 | 依 OD-04 核准範圍修正 Python，建立可重現契約、外部 bridge 證據、雙平台基線及測試能力清單 |
| Stage 1 | 完成 Rust CLI，搭配已通過 Stage 0 的 Python daemon，形成可安裝、可監管、可更新、可回滾的正式混合組合 |
| Stage 2 | 以四個垂直切片移植 Rust daemon；從 Slice 2.1 起就具備最小 Quiet／Drain／Release 與禁止危險重送能力 |
| Stage 3 | 通過雙平台實機長跑、Canary 與回滾演練後，退役產品執行期中的 Python daemon |

**Stage 1 是完整發佈組合的檢查點，不只是交付一個 Rust CLI。**其 artifact 必須包含或明確管理相容的 Python daemon 執行環境、設定、監管入口與回滾資產；不得依賴測試人員自行尋找未固定版本的 Python 安裝。

Stage 2 是計畫目標。依 OD-02，Stage 0／1 出口通過且 OD-04、OD-10、OD-13 前置完成後，直接啟動 Slice 2.1，並以實測速率校準後續切片。觸發斷路器時，只能退回已完成驗收的 Stage 1 artifact 組合，不能以當時任意的 Python 分支作為退路。


### 0.3 事實、提案、核准與驗收狀態

本文件使用下列狀態：

| 狀態 | 意義 |
|---|---|
| `EXISTING` | 已由 repo 現況或既有部署行為確認；不等於沒有缺陷 |
| `PROPOSED` | 設計或變更提案，尚未獲擁有者核准 |
| `APPROVED` | 已有擁有者裁決紀錄，包含範圍、條件與適用版本 |
| `IMPLEMENTED` | 已有對應實作 artifact，但尚不能推導所有驗收通過 |
| `VERIFIED` | 指定 artifact 在指定環境與案例集合取得有效通過證據 |

BC-01～BC-05 已於 2026-10-15 依 OD-04 全數核准為 `APPROVED`，核准範圍以 §9.1 為準。

第 11、12、15 章的新增資源承諾、長跑發版 SLO 及 Canary 分平台配額，已依 OD-09、OD-10、OD-13 裁決（第 14 章）。OD-09 延後凍結的項目在凍結前、以及證據不足的必要條件，不得默認通過。

契約、案例、工具、artifact 或工作負載變更後，須判定既有證據是否仍有效；不得只因版本字串相同就沿用先前 PASS。

## 1. 目標與非目標（含四大痛點量化驗收指標）

### 1.1 標準量測環境與 Workload-S1

#### 1.1.1 環境識別

Linux 與 Windows 分開建立結果，不合併平均。Linux 原生與 WSL2 亦須分開標示，不能以 WSL2 結果取代原生 Windows serial backend 驗證。

量測 manifest 至少記錄：

`OS/build`、CPU、記憶體、檔案系統、儲存裝置、電源設定、虛擬化方式、端點防毒產品與狀態、CLI／daemon artifact、契約 revision、工具 revision、計時方法與串口／驅動資料。

v4 的 Ubuntu 22.04／24.04、Windows 11、4 核心或 vCPU、8 GB RAM、NVMe，保留為初始參考配置。依 OD-05／OD-08，Windows 代表機台兼自架 runner 為擁有者本機：Windows 11 Pro 24H2、HVCI 開啟、Secure Boot 關閉、使用者具管理員權限、端點防毒為 第三方企業端點防毒（EDR）（Windows Defender 關閉）。若測試團隊機台的 Secure Boot 等設定不同，須另行記錄並分開報告。本文件不將某個 `*-latest` CI 標籤視為固定硬體環境。

#### 1.1.2 Workload-S1 分成兩個可區分的測試

**S1-W：WAL 寫入微基準。**

使用實際受測 WalWriter，建立四個獨立寫入流；每流寫入 10,000 筆、每筆 1,024 bytes 原始 payload。每流目標資料速率為 10,240 bytes/s，輸入包含固定比例的 ANSI、一般文字及 prompt 片段。

此測試固定「傳入 WalWriter 的紀錄大小」，用於量測序列化、CRC、兩個檔案寫入及每筆 flush 的成本。它是組件性能測試，不列為外部 UART 端到端證據。

**S1-E2E：四 Session 端到端串流。**

四個 Session 以 115200、8N1 接收相同資料內容及每埠目標速率，使用受測 CLI／daemon／serial backend。量測實際 RX、WAL、Console 延遲與完整性。

此測試不得要求 OS read chunk、WAL 筆數或單筆長度恰好等於 S1-W；比較依連續 byte stream 與實際工作量進行。

OD-13 已核准本節明列的負載數值與量測邊界；尚未列值的 ANSI／文字／prompt 比例、資料 fixture、暖機、取樣算法、Console 樣本分布及工具開銷量測方式，須由 Stage 0 測試負責人在首次正式量測前具體化並經獨立審閱，保存於固定 revision 與 hash 的量測 manifest。Python／Rust 比較須使用同一口徑，不得看完結果後更改。

#### 1.1.3 Rotate 條件

產品 rotate 門檻的實際 byte 值，以 Stage 0 擷取的 Python 常數為準，記為 `R`；不得自行將 v4 的「64 MB」解讀成 64,000,000 或 67,108,864 bytes 後改動產品行為。

量測前，每個 WAL 預填至接近 `60/64 × R`，保存實際起始大小。預填內容必須可解析，不得用無效 padding 冒充 WAL。

每個寫入流須實際跨越至少一次 rotate。報告保存每次 rotate 的耗時與最大值，不能只挑選其中一次。檔案大小依實際序列化 bytes 計算，不等同原始 payload 大小。

#### 1.1.4 取樣與量測邊界

計時使用同一程序可比較的單調時鐘。不同程序或主機的單調時間不得直接相減。

CLI 測試包含程序建立至 stdout／exit 完成；Console 測試明確定義輸入與對端回顯的觀測點。暖機、取樣數、分位數算法及量測工具開銷須在執行前固定。

一般寫入、正常 rotate、外部 unlink、外部 reader 阻擋及磁碟錯誤屬不同情境；必須分開報告，不能在測試後任意排除較慢樣本。

### 1.2 四大痛點與量化驗收指標

下列性能數值沿用 v4 目標，不代表指定 artifact 已達標；量測口徑依 OD-13 執行。本表作為 §11.6／§12.2 性能出口的判定依據，§13 的實測用於確認達標與可行性，不具有自行取消門檻的效力。未達標時保留失敗結果並依本節規定提請擁有者重新裁決，不得由執行者改列觀察值或回填 PASS。

| 指標 | Windows 目標 | Linux 目標 | 量測口徑 |
|---|---:|---:|---|
| WAL 一般單筆寫入 p50 | ≤0.03 ms | ≤0.005 ms | S1-W；包含指定的兩檔寫入與每筆 flush，不含 rotate 當筆 |
| WAL 一般單筆寫入 p95 | ≤0.08 ms | ≤0.01 ms | 同上 |
| WAL 一般單筆寫入 p99 | ≤0.20 ms | ≤0.02 ms | 同上 |
| 正常 rotate 最大停頓 | ≤15 ms | ≤3 ms | 保存所有 rotate 樣本；包含本次 rotate 規定的同步操作 |
| CLI 暖快取往返 p50 | ≤5 ms | ≤2 ms | daemon 已運行，200 次 `daemon status` |
| CLI 暖快取往返 p95 | ≤10 ms | ≤3.5 ms | 同上 |
| CLI 暖快取往返 p99 | ≤15 ms | ≤5 ms | 同上 |
| Console 按鍵至回顯 p50 | ≤3 ms | ≤3 ms | 115200 bps 實體 loopback，500 個可識別輸入樣本 |
| Console 按鍵至回顯 p99 | ≤10 ms | ≤10 ms | 不使用 Docker／Python bridge 的耗時取代實體 loopback |

v4 所引用的 Windows WAL 平均 16.8 ms、p99 26 ms，以及 Python CLI 約 90～110 ms，屬原始歷史量測；須保留原環境與測試方法，不能直接與不同口徑的新結果相除後宣稱改善倍數。**更正（OD-05）**：該 16.8 ms 量測所在主機的端點防毒為 第三方企業端點防毒（EDR），Windows Defender 並未執行；先前文件將檔案掃描成本歸因於 Defender 為誤植。「每筆開關檔為主因、常駐開檔可解決」的結論不變。

其他驗收指標如下：

| 維度 | 必要條件 |
|---|---|
| Windows 安裝與發佈 | Rust CLI 不採 one-file 執行期解壓；連續 100 次 CLI 執行，不因 CLI 啟動新增解壓執行檔或解壓目錄 |
| 端點防毒 | 在指定 artifact、指定 runner、指定測試期間，實際端點防毒（代表機台為 第三方企業端點防毒（EDR）；Windows Defender 關閉）沒有新增攔截；查詢權限不足或資料不可取得時列證據不足，不得填零 |
| Telnet Console | 跨 chunk IAC、入向 NVT 摺疊、多 Console、Raw／Line ownership 與 DeferredBuffer 案例通過 |
| Serial Console | 依 OD-01 為**選配**，僅在驅動可部署的機台啟用；啟用時須在合格機台驗證逐字元互動與實體控制線隔離。驅動無法部署不阻擋 Windows 發佈 |
| 契約治理 | 所有生效的 RPC／CLI `error_code` 均有完整處置資料；session `last_error` 另有獨立目錄 |
| 平台覆蓋 | 雙平台 L1 全綠不取代原生 Windows COM gate；適用 L1.5、L2、L3 依第 11、15 章完成 |

原生 binary 不構成「未來永不被防毒誤判」的保證。性能未達標時須保留結果、分析原因並重新裁決；不得先降低門檻再回填 PASS。

### 1.3 非目標與相容性邊界

本專案不以重構為由刪除既有 20 個 CLI 頂層指令群組、RPC 功能、Telnet Console、CommandArbiter、Login FSM、Interactive Lease、開機過渡 Gate、Event Engine、File Transfer 或適用平台的 MCU flash 能力。

既有 CLI／RPC／WAL／`state.json` 的外部欄位與語意以 Stage 0 契約為基準。必要修正須登記 BC 或其他經核准的契約變更；「不得破壞相容性」不代表必須複製已核准要修復的缺陷。

本專案不承諾：

- 在任意驅動、特權程序或未受控外部工具存在時，仍能提供無條件的硬體獨占保證。
- 以 PTY／虛擬 COM 證明真實 UART 的電氣特性或所有 USB 拔插語意。
- 在 UART 寫入與 WAL append 之間提供跨裝置原子交易或 exactly-once 裝置執行。
- 為了移除產品 Python daemon，同時移除 Python 測試工具、既有 TestPilot 引擎或 bridge。

## 2. 架構決策紀錄

### ADR-01：Rust 遷移與能力邊界

採 Stage 0 Python 基線、Stage 1 Rust CLI、Stage 2 Rust daemon 垂直切片、Stage 3 Canary 與退役的漸進路線。啟動範圍依 OD-02，變更範圍依 OD-04、OD-13。

Rust 的主要價值為原生部署、型別與所有權約束，以及可集中審查的平台邊界；不能由語言選擇推導 Windows 執行期、驅動取消、硬體交接或業務競態已正確。

跨目標 `cargo check` 用於早期型別檢查，不取代原生 Windows build、link 與 runtime 測試。

Regex 使用受限制的模式大小與輸入長度，並依實際 API 驗證性能。`regex` 的單次搜尋具有 `O(m×n)` 界限；不能把反覆搜尋或所有 matches 的迭代一律宣稱為線性時間。[Docs.rs](https://docs.rs/regex/latest/regex/)

### ADR-02：雙層互斥與受控部署範圍

保留 Tier 1 實例鎖與 Tier 2 Per-UART 獨占，允許不同隔離實例管理不同裝置；禁止以全機單一 daemon 鎖破壞 sandbox 測試。

`--force-spawn` 只允許略過 CLI 的跨 socket 多實例預檢，不得略過實例鎖、Per-UART 鎖、裝置 allowlist 或交接屏障。

POSIX 使用 `flock`、`TIOCEXCL` 與 holder 診斷的組合，但保證範圍限定於第 6 章的受控部署条件；不得宣稱可撤銷既有外部 handle 或阻止所有特權程序。

Windows 透過正常通訊裝置的獨占開啟取得 handle。占用與不明原因存取失敗必須如實診斷，不能以空 PID 清單推導沒有 holder。

BC-04 已依 OD-04 核准（`APPROVED`，2026-10-15）。

### ADR-03：Console 產品能力與 Serial 測試能力分離

Telnet 為 Windows 免驅動基準 Console。依 OD-01，Tera Term Serial Console 為選配，僅在驅動可部署的機台啟用。

L1.5 的 DUT-side 虛擬 COM pair 與產品 Console-side pair 是不同用途。OD-01 已將 Serial Console 定為選配，但Windows 原生 serial backend 仍須取得必要測試證據；測試資源由 OD-08 決定。

虛擬驅動採用資格、端點角色及装置選取限制依第 7、13、15 章。

### ADR-04：IPC 傳輸與信任模型

保留 Linux 路徑型 UDS 與 Windows `127.0.0.1` TCP，維持既有 CLI 及 SSH tunnel 相容性。

Linux UDS 使用受控目錄與 `0600` socket 權限。Windows TCP loopback 僅描述網路可達範圍；目前設計沒有依連線驗證 Windows 使用者身分，因此不能僅憑 `ProgramData` ACL 宣稱建立「同作業系統使用者」RPC 邊界。連線身分驗證與授權是須另行設計的能力。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-secure-socket-extensions)

OD-12 定義兩種部署路線，**2026-10-15 裁決採「整機本機使用者可信」並僅部署於單人使用的工作站**：

| 路線 | 條件 |
|---|---|
| 整機本機使用者可信 | 明確接受本機其他使用者可連到 loopback 的風險；僅部署於符合此信任範圍的工作站／bench |
| 多使用者隔離 | 在部署前完成實際的連線認證與授權，涵蓋 RPC、Console、CLI、remote tunnel 與憑證生命週期 |

未完成多使用者隔離前，不得將無認證 loopback RPC 的 `windows-service` 部署描述為已隔離不同本機使用者，**多人共用機器（含無人值守 `windows-service` bench）亦不得部署**。規格不得宣稱已具「同使用者隔離」。`serialwrap remote` 經 ssh 暴露 RPC 時，信任範圍延伸至通過該 ssh 驗證者。不得為保持文字相容而隱藏此限制。

### ADR-05：WAL 常駐開檔與分層證據

BC-01 已依 OD-04 核准改為常駐檔案 handle、每筆 flush、正常 rotate／關機同步，以及外部 unlink 後的有界自癒；完成狀態仍以實作與驗收證據判定。

RX WAL 表示 daemon 從 OS handle 讀到並完成記錄的 bytes；TX WAL 表示已確認完成的 OS 寫入部分，不等同 DUT 已收到或已執行。

UART write 與 WAL append 不是原子交易。外部 bridge 提供獨立對端觀測，不能由產品 WAL 自己證明所有送收都已發生。

Windows 自癒不得假設關閉 daemon 自身 handle 後，其他程序的 handle 亦已消失；詳細協定依第 8 章。

### ADR-06：單寫入者、授權失效與 Drain Barrier

所有 TX 來源均通過單一受控寫入路徑。入列檢查不能取代實際開始寫入前的 epoch、owner、Gate 與裝置生命週期檢查。

從 Slice 2.1 起提供最小 Quiet Gate、在途命令分類、Drain、Release、禁止危險重送及有界背壓。完整 Console／Login／flash 功能可以分期承接，安全骨架不能後移。

取消與等待 I/O 完成在 PAL／worker 執行；Actor 必須能持續處理診斷與 timeout。過期事件不得更新新的 Session 狀態，但其中已發生的 I/O 證據不得被丟棄。

BC-02、BC-03 已依 OD-04 核准（`APPROVED`，2026-10-15）。

### ADR-07：契約先行與獨立驗證

分別凍結產品回應、session 診斷、WAL、持久化、測試能力與案例清單。

RPC／CLI `error_code` 與 session `last_error` 為不同命名空間。`CREDENTIALS_UNRESOLVED` 屬後者，不納入 RPC 錯誤碼計數。

L1.5、L2、L3 維持黑箱邊界，不以產品的 Serial、WAL、仲裁或 Login FSM 實作作為 oracle。允許共用宣告式 schema、案例資料及經審閱的測試專用 helper。

Python→Rust 差分採新的相同初始 DUT 狀態與 Sequential A/B；先驗證各自的絕對契約，再比較差異。

### ADR-08：Agent 治理與可追溯發佈

錯誤處置目錄、`SKILL.md`、artifact 身分與驗收證據共同形成治理基礎。

本版候選 RPC／CLI 錯誤碼集合為 78 個，其中 `COMMAND_ABORTED_IN_FLIGHT` 已依 OD-04 核准；候選集合依核准範圍維護，各發佈 artifact 的生效集合則依其可輸出的程式路徑、契約 revision、實作及契約測試證據產生。已核准但未實作的碼不得列為生效；實作輸出未核准的碼時必須判定不合格，不得從掃描結果隱藏。計數由資料產生，不硬編碼為 78。

測試工具的 `HARNESS_*` 原因碼不得混入產品錯誤碼。候選產品不得透過未審閱的 oracle 修改自我驗收。

政策實作位置依 OD-06，bridge 位置依 OD-07；二者不影響「產品與判定邏輯分離」的要求。

## 3. 架構分層與 Cargo Workspace 結構

### 3.1 平台條件編譯與依賴隔離

平台相依的 production code 限於 `sw-pal`。其他 crate 不得透過 `#[cfg(unix)]`、`#[cfg(windows)]`、`target_os`、`cfg!`、`cfg_attr`、條件模組或 build script 間接加入不同平台的業務分支。

允許正常的 `#[cfg(test)]`，以及經審閱、不造成平台業務差異的測試或功能設定。Rust 的 `cfg(test)` 與平台條件是不同用途，不應以「禁止所有 `#[cfg]`」混為一談。[Rust 文件馆](https://doc.rust-lang.org/reference/conditional-compilation.html)

`sw-core` 只依賴平台中立的能力介面與資料型別，不直接依賴 Win32、POSIX 裝置、檔案 identity 或程序列舉 API。某項能力不適用時，由組裝層提供明確 capability；不得在業務層以 OS 名稱分支。

能力缺失不得轉為靜默成功。CLI／RPC 的外部表示依第 9 章凍結，不能由實作者臨時新增錯誤碼。

L1 白箱單元測試可直接測產品模組。L1.5、L2、L3 及黑箱契約比較器不得 import／連結產品內部實作作為判定邏輯。

### 3.2 Workspace、契約與既有測試資產

下列為 v6 目標結構；新增路徑不代表目前 repo 已存在。依 OD-07，bridge 固定置於本 repo 的 `tools/uart_stdio_bridge/`，採獨立依賴、自測及審閱邊界。

```text
serialwrap/
├── Cargo.toml
├── contracts/
│   ├── error_codes.yaml
│   ├── session_last_errors.yaml        # 新增：與 RPC error_code 分離
│   ├── behavior_changes.yaml           # 新增：BC 狀態與核准紀錄
│   ├── rpc_schema.json
│   ├── cli_schema.json
│   ├── wal_record.schema.json
│   └── state_file.schema.json
├── crates/
│   ├── sw-proto/src/
│   │   ├── lib.rs
│   │   ├── constants.rs
│   │   ├── error_codes.rs
│   │   ├── rpc_types.rs
│   │   ├── config_types.rs
│   │   └── wal_types.rs
│   ├── sw-pal/src/
│   │   ├── lib.rs
│   │   ├── traits.rs
│   │   ├── sysenv.rs
│   │   ├── serial.rs
│   │   ├── device_source.rs
│   │   ├── device_watcher.rs
│   │   ├── lock.rs
│   │   ├── wal_fs.rs
│   │   ├── rpc_transport.rs
│   │   ├── console_pty.rs
│   │   ├── console_com0com.rs
│   │   ├── service_ctl.rs
│   │   └── process_tunnel.rs
│   ├── sw-core/src/
│   │   ├── lib.rs
│   │   ├── session_manager.rs
│   │   ├── uart_bridge.rs
│   │   ├── telnet_console.rs
│   │   ├── arbiter.rs
│   │   ├── login_fsm.rs
│   │   ├── wal.rs
│   │   ├── file_transfer.rs
│   │   ├── flash_endpoint.rs
│   │   ├── transport_stall.rs
│   │   ├── event_engine/
│   │   ├── alias_registry.rs
│   │   ├── bench_registry.rs
│   │   ├── state_migrate.rs
│   │   └── session_auth.rs
│   ├── sw-daemon/src/
│   │   ├── service.rs
│   │   └── daemon_main.rs
│   └── sw-cli/src/
│       ├── main.rs
│       ├── client.rs
│       ├── remote_tunnel.rs
│       ├── doctor_cmd.rs
│       ├── setup_cmd.rs
│       └── cli_trace.rs
├── tests/
│   ├── contract_blackbox/
│   └── acceptance/                     # 新增：清單與 schema，非新框架
│       ├── cases.yaml
│       └── bridge_record.schema.json
├── func-test/                          # 保留既有 YAML 框架與 FakeTarget
├── regression/                         # 保留 serialwrap_regression
├── realhw/                             # 保留穩定性／longrun 引擎
└── tools/
    ├── docker/uart_harness.py           # 保留既有 remote 拓樸驗收
    └── uart_stdio_bridge/              # OD-07 核准位置
```

測試流程仍由既有 `func-test`、TestPilot／`regression` 與 `realhw` 管理。`tests/acceptance/` 只保存案例承接清單、能力需求及證據 schema，不建立另一個排程、重試或報告引擎。

產品 crate 不得依賴 bridge 或 DUT 模擬器。bridge 不得依賴 `sw-pal`；否則產品與外部觀測可能共用同一個 Serial 缺陷而失去獨立性。

## 4. Python → Rust 完整模組對照與 CLI 20 個頂層指令群組分期表

### 4.1 Python 模組責任與 Rust 承接順序

以下保留 v4 的模組責任盤點，修正承接順序。行數屬歷史估算，不作完成率或工期的唯一依據。

| Python 模組 | Rust 責任位置 | 承接階段 |
|---|---|---|
| `session_manager.py` | `sw-core::session_manager`；狀態、排序、Lease、Capture、Recovery | 2.1 起具最小安全狀態與交接；2.2～2.3 完整承接 |
| `cli.py` | `sw-cli::main`；20 群組路由與 JSON | 1A／1B |
| `uart_io.py` | `sw-core::uart_bridge`＋PAL Console／Serial | 2.1 受控 RX／TX、取消與證據；2.2 完整 Human Console |
| `service.py` | `sw-daemon::service`；唯一 RPC 分派與組裝 | 2.1 起 |
| `remote_tunnel.py` | `sw-cli::remote_tunnel`＋`sw-pal::process_tunnel` | Stage 0 的 BC-05；1B |
| `doctor_cmd.py` | `sw-cli::doctor_cmd` | 1B |
| `setup_cmd.py` | `sw-cli::setup_cmd` | 1B |
| `file_transfer.py` | `sw-core::file_transfer` | 2.4 |
| `serial_port.py` | `sw-pal::serial`；獨占、部分完成、取消、關閉 | **2.1 必備** |
| `wal.py` | `sw-core::wal`＋`sw-pal::wal_fs` | Stage 0 的 BC-01；2.1 |
| `config.py`／`runtime_config.py` | `sw-proto::config_types`；設定解析與契約 | 1A 起 |
| `arbiter.py` | `sw-core::arbiter`；Admission、單寫入者與送出前檢查 | **2.1 必備** |
| `flash_endpoint.py`／`mcu_patterns.py` | `sw-core::flash_endpoint`＋PAL | 2.4 |
| `login_fsm.py` | `sw-core::login_fsm` | **2.1 最小 Shell／Quiet／帳密終態**；2.3 完整 profile 與重驗 |
| `client.py` | `sw-cli::client`；11 方法重試白名單 | 1A |
| `sysenv.py` | `sw-pal::sysenv` | 1A |
| `device_source.py`／`device_watcher.py` | PAL 裝置發現與生命週期 | 2.1 |
| `constants.py` | `sw-proto::constants` | 1A 起 |
| `daemon.py` | `sw-daemon::daemon_main` | 2.1 |
| `bench_registry.py`／`alias_registry.py`／`state_migrate.py` | `sw-core` 對應模組 | 2.1 |
| `cli_trace.py` | `sw-cli::cli_trace` | 1A |
| `multi_open.py`／`daemon_lock.py`／`lock_posix.py`／`lock_win.py` | `sw-pal::lock` | 1B 的管理面；2.1 daemon 獨占 |
| `telnet_console.py` | `sw-core::telnet_console`＋平台中立 transport | 2.2 |
| `event_engine/` | `sw-core::event_engine` | 2.3 |
| `auth.py` | `sw-core::session_auth`；DUT 帳密解析，不是 RPC 認證 | 2.1 |
| `transport_stall.py` | `sw-core::transport_stall` | 2.1 最小 I/O 故障中止；2.3 完整 Stall 策略 |
| `service_ctl.py`／`systemd_units.py` | `sw-pal::service_ctl` | 1B；2.4 整合驗收，不延後基本停止安全性 |
| `platform_backends.py`／`rpc_posix.py`／`rpc_win.py` | `sw-pal` 選擇器與 RPC transport | 1A client；2.1 server |

`device release/attach` 的最小安全路徑前移到 2.1：停止新 TX、Drain、關閉、保持 RELEASED、明確重新取得裝置。2.4 承接的是完整外部工具／flash／監管交接整合，而非第一次實作安全釋放。

### 4.2 全域選項、`--retries` 白名單與 20 個頂層 CLI 指令群組分期表

#### 1. 全域選項與 `RETRYABLE_READONLY_METHODS` 嚴格白名單

* 
**全域選項**：`--socket <PATH>`、`--endpoint tcp://<HOST>:<PORT>`、`--bench <CODE>`、`--timeout <SEC>`、`--retries <N>`。

* 
**`--retries` 唯一允許自動重試之 11 個 RPC 方法白名單（對齊 `client.py::RETRYABLE_READONLY_METHODS`）**：

* 
`health.ping`

* 
`health.status`

* 
`session.list`

* 
`session.get_state`

* 
`session.activity`

* 
`session.console_list`

* 
`session.log_status`

* 
`device.list`

* 
`alias.list`

* 
`mcu.patterns`

* 
`mcu.status`

**嚴格限制**：不在上述 11 個方法內之任何 RPC 呼叫（包含 `cmd.status`、`cmd.result_tail`、`wal`、`benches`、`profile`，以及**會 touch 並延長租約有效期、具狀態副作用的 `session.interactive_status`**），即使命令列帶入 `--retries` 亦**絕對不執行自動重試**。

#### 2. 20 個頂層 CLI 指令群組之 Stage 1A / Stage 1B 分類
序號頂層指令群組實際子命令與參數歸屬階段實作與行為說明1**`cmd`**`submit` (`--mode line|background|interactive`), `status`, `result-tail`, `cancel`**Stage 1A**純 RPC 呼叫。2**`session`** *(RPC 類)*`list`, `clear`, `bind`, `pin`, `unpin`, `attach`, `self-test`, `activity`, `recover`, `console-list`, `interactive-open` (`--allow-attached`), `interactive-send`, `interactive-status`, `interactive-close`, `log-start`, `log-stop`, `log-status`**Stage 1A**純 RPC 呼叫。—**`session`** *(終端類)*`console-attach`, `console-detach`**Stage 1B**涉及本地終端機橋接互動，歸入 Stage 1B。3**`device`**`list`, `release`, `attach` *(收回裝置為 `device attach`)***Stage 1A**純 RPC 呼叫。4**`alias`**所有別名查詢與設定子命令**Stage 1A**純 RPC 呼叫。5**`file`**`push`, `pull`**Stage 1A**本地讀寫檔案＋RPC 傳輸呼叫。6**`wal`**所有 WAL 查詢、reset 與匯出子命令**Stage 1A**純 RPC 呼叫。7**`log`**所有日誌操作子命令**Stage 1A**純 RPC 呼叫。8**`stream`**所有即時串流讀取子命令**Stage 1A**RPC 串流讀取。9**`mcu`**`patterns`, `status`**Stage 1A****純唯讀查詢 RPC**（查詢 MCU 識別特徵與目前狀態，不控制常駐被動之 `/dev/ttyMCU`）。10**`event`**所有事件規則管理子命令**Stage 1A**純 RPC 呼叫。11**`benches`**所有測試台查詢子命令**Stage 1A**純 RPC / 設定查詢。12**`profile`**所有 Profile 查詢子命令**Stage 1A**純 RPC / 設定查詢。13**`skill`**輸出內建 `SKILL.md` 指引**Stage 1A**直接輸出內嵌資產，零外部依賴。14**`daemon`**`start` (`--force-spawn`), `stop`, `restart`, `status`**Stage 1B**涉及本地行程管理、多開偵測（`DAEMON_ALREADY_RUNNING_ELSEWHERE`）與監管轉送。15**`service`**系統服務控制子命令**Stage 1B**呼叫 `service_ctl` 管理監管服務。16**`supervision-mode`**查詢與切換 `config.yaml` 之 `supervision_mode`**Stage 1B**修改設定並執行監管 reconcile。17**`remote`**`-R` (expose), `-L` (connect), `status`, `stop`**Stage 1B**本地管理 OpenSSH 轉發行程與 Registry。18**`connect`**遠端／測試台連線輔助子命令**Stage 1B**本地端點與通道組合管理。19**`setup`**資產物化、監管 reconcile、`--rollback`**Stage 1B**本地環境初始化與升降版管理。20**`doctor`**唯讀環境、鎖與 Daemon 健康檢查**Stage 1B**本地系統檢查＋RPC 探測。
*(註：Stage 1A 期間，當使用者執行第 14～20 項及 `session console-attach/detach` 時，Rust CLI 自動透明委派予同環境之 Python 入口執行。)*


### 4.3 Stage 1 混合組合與透明委派約束

Stage 1A 尚未承接的管理／終端命令，依 §4.2 委派到**同一發佈組合中固定的 Python 入口絕對路徑**。不得從不受控的 `PATH` 搜尋另一版 serialwrap，也不得因入口設定形成 Rust→Python→Rust 遞迴。

委派必須保留參數、stdin／stdout／stderr、exit code、timeout 與必要的訊號／取消行為；不得自行重送具有副作用的命令。

Stage 1B 出口後，CLI 管理與終端能力依承接清單完成 Rust 化；Python daemon 仍是明列的 runtime component。版本相容與 artifact 身分依 §9.5。

## 5. 核心型別、事件模型、完整狀態 × 事件轉移表與背壓設計

### 5.1 核心狀態、世代與事件身分

#### 5.1.1 對外 Session 狀態

保留：

`DETACHED`、`ATTACHING`、`ATTACHED`、`READY`、`RECOVERING`、`RELEASED`、`FLASHING`。

Quiet、Ready Reconfirm、Drain Stalled 與帳密未解析不自動新增 SessionState。其對外欄位與狀態組合依第 9 章凍結。

#### 5.1.2 世代

| 名稱 | 責任 |
|---|---|
| `conn_epoch` | 隔離不同 I/O worker／裝置連線生命週期；取消舊連線、釋放或建立新連線時依明確操作遞增 |
| `fsm_gen` | 隔離登入、Quiet、Ready Reconfirm 的步驟與計時器 |
| `owner_gen` | 隔離 Human／Agent ownership 變更及 suspend／resume 上下文 |

同一生命週期操作只在指定線性化位置更新世代，不得因經過數個狀態名稱而重複遞增。登入步驟更新不得順便重設實體連線；ownership 切換也不能假裝舊 I/O 已完成。

Flash 認線以 `probe_id` 識別；Console 連線以 `client_id` 及連線生命週期識別；命令以 `cmd_id` 與受測 daemon 實例識別。這些身分不得互相代用。

#### 5.1.3 事件集合與必要上下文

原有事件責任保留，內部 payload 須足以拒絕過期操作：

| 事件群組 | 事件與必要資料 |
|---|---|
| 裝置／I/O | `DeviceAttached`、`DeviceDetached`、`UartRxChunk`、`UartTxCompleted`、`UartIoError`；包含裝置／worker 身分、epoch、實際資料或完成結果 |
| Drain | `WorkerDrainTimeout`、`WorkerStoppedReceipt`；包含舊 worker、舊 epoch、停止操作身分、完成狀態與 handle 關閉確認 |
| 開機／登入 | `BootBannerDetected`、`BootQuietWindowExpired`、`LoginFsmStepTimeout`、`ReadyProbeVerified`、`ReadyReconfirmExhausted`；包含適用 epoch、`fsm_gen`、step／nonce |
| 命令 | `CmdSubmit`、`CmdStepTimeout`、`CmdCancel`、`CmdResultTail`；包含 request／`cmd_id`／reply，timeout 另含操作世代 |
| Console | `ConsoleConnected`、`ConsoleDisconnected`、`ConsoleIdleTimeout`、`ConsolePeerGraceExpired`、`ConsoleInputBytes`；包含 client 生命週期及擷取輸入時的 ownership 上下文 |
| Interactive Lease | `InteractiveOpen`、`InteractiveSend`、`InteractiveStatus`、`InteractiveClose`、`InteractiveExpired`；包含 Lease 身分；到期事件還須核對目前 deadline |
| Flash | `FlashSyncProbeStart`、`FlashSyncMatched`、`FlashProbeReleased`、`FlashHolderGone`；包含 `probe_id` 或適用 epoch |
| 控制與其他 | `DeviceRelease`、`DeviceAttach`、`SessionRecover`、`SessionRecoverForceTimeout`、`SessionSelfTest`、`TransportStallDetected`、`LogCaptureControl`、`FileTransfer`；由 Actor 指定當次操作上下文 |

上述新增內部身分不直接成為 RPC／WAL 頂層欄位；需要對外提供時須先走契約核准。

#### 5.1.4 TX 完成結果

內部必須區分：

| 結果 | 含義 |
|---|---|
| 尚未提交 OS I/O | 可確認沒有由該操作發起寫入 |
| 已確認完成 N bytes | 只對確認完成部分建立 TX WAL；N 可小於要求長度 |
| 完成結果不明 | 不能證明零 TX，也不能虛構已完成長度；禁止自動重送 |

BC-03 已核准 `COMMAND_ABORTED_IN_FLIGHT` 的語意為「已確認至少寫出 1 byte 後中止」。結果不明不得未經核准地擴張此碼語意，也不得降格成可安全重試的 `FLUSHED_BY_RECOVERY`；其既有錯誤碼映射及診斷欄位須在 Stage 0、Slice 2.1 出口前凍結。

### 5.2 單寫入者、送出前檢查與 Two-Phase Drain Barrier

#### 5.2.1 TX 來源與 Gate

| TX 來源 | 允許範圍 | Quiet 與 ownership 規則 |
|---|---|---|
| FlashEndpoint | 合格候選態認線；`FLASHING` 專用橋接 | 不受一般 Quiet 阻擋，但須具認線／Flash 專用授權，不得與一般命令並行 |
| Agent Interactive Lease | `READY`；符合救援條件的 `ATTACHED` | 有效 Lease 可中斷 autoboot；不是任意繞過獨占與 Drain |
| Human Console | `ATTACHING`、`ATTACHED`、`READY` | Raw／Line 依順序及 ownership；被 suspend 時进入 DeferredBuffer，不直接送 UART |
| Login FSM／Ready Probe | 適用登入與重驗態 | Quiet、帳密未解析終態、Drain 或失效世代期間禁止自動 probe |
| CommandArbiter／FileTransfer | `READY`；核准的 passthrough `ATTACHED` 例外 | 受 Quiet、Reconfirm、Lease、容量與裝置交接 Gate 限制 |

所有來源共用同一受控 Serial writer；「不受 Quiet 阻擋」不等於可以繞過 writer 或在 handle 關閉期間送資料。

#### 5.2.2 三個檢查位置

命令至少在 Admission、出列、開始 OS 寫入前三個位置核對必要狀態。後兩個位置必須重新檢查 epoch、owner、Quiet／Reconfirm、Release／Recovery 及取消狀態。

Writer 的授權與 fence 失效須有可測試的序列化邊界。禁止採用「讀一次旗標，過一段時間再 write」而沒有處理其間狀態變更。

已提交 OS 的 I/O 可能在 fence 後才完成；此部分須按在途操作記錄，不能宣稱取消能撤回已發生的 bytes。外部 zero-TX assertion 應针对未取得寫入授權或可確認未提交的操作。

Reboot Gate 至少包含切片已支援 profile 的 banner／Quiet 辨識。尚不支援的 profile 不能以關閉 Gate 的方式通過 Slice 2.1。

#### 5.2.3 Phase 1：停止新授權與取消

Actor 進入交接中的內部狀態，停止新 TX 授權，使舊 epoch／owner 的未執行操作失效，清理依契約不可保留的佇列、Lease 與 DeferredBuffer。

對舊 worker 發出停止要求。POSIX 使用可喚醒的讀取機制；Windows 對適用的 overlapped I/O 提出取消。

`CancelIoEx` 是取消要求，不保證驅動立即或成功取消；在確認完成前不得重用相關 OVERLAPPED 結構或 buffer。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/fileio/canceling-pending-i-o-operations)

#### 5.2.4 Phase 2：完成收斂與關閉回條

PAL／worker 等待所有在途 I/O 收斂，保存可確認的完成結果及結果不明的範圍，完成可執行的證據記錄後關閉 handle，再發出 `WorkerStoppedReceipt`。WAL 無法寫入時，回條須明列未落盤證據及失敗原因，不得等待 WAL 恢復才關閉已可安全關閉的 handle，也不得因此宣稱證據完整；尚未確認 I/O 收斂或 handle 關閉時仍不得發出成功回條。此失敗分支須納入 Stage 0 與 Slice 2.1 驗收。

等待不得阻塞 SessionActor 的 mailbox。Actor 必須能回應狀態查詢、標示 timeout、拒絕新工作及保留診斷。

舊 epoch 的 completion 不得改變新 Session 狀態，但仍須按原 operation 身分處理實際完成 bytes；不能因「過期事件」而遺失或重複記錄。

收到並驗證回條前：

- 不得另開同一受管端點。
- 不得將 `device release` 回覆為成功。
- 不得將尚未確認完成的操作當成零 TX。
- 不得以殺掉 worker 物件或清空佇列代替 OS handle 關閉證據。

#### 5.2.5 Timeout 與後續處置

超過 3,000 ms 未完成 Drain 時，Session 保持 `RECOVERING`，並提供 `drain_stalled: true`、`drain_timeout_epoch`。這是 v5 設計值，須在雙平台實測。

`session recover --force` 未在既有時限回到 READY 時才回 `FORCE_RECOVER_TIMEOUT`；Drain timeout 本身不冒用此碼作內部事件名。

停止操作須去重。重複 `release`／`recover` 不得啟動多組關閉或重新開啟程序。呼叫端 timeout 不等於停止操作已結束；後續行為依實際 worker 狀態判斷。

#### 5.2.6 DeferredBuffer

未發生上下文失效的 Soft Preempt 結束後，保留 #53 正常還原與回放。

遇 Reboot／Quiet 重新啟動、Recovery、Release、Flash 或容量溢位時，依 BC-02 丟棄整段舊輸入。輸入擷取時的 owner／connection 上下文須保留，避免已在 mailbox 排隊的舊按鍵於清空後再次補入。

Flash Sync 的授權切換不能為了實作世代而任意破壞已取得的 ACK 或重開不必要的連線；同 handle 模式切換與實際 handle 更換須分開建模及驗證。

### 5.3 狀態轉移與事件拒絕規則

安全／交接事件須具有有界處理延遲，但不能以「優先序」破壞同一來源的必要順序。例如不得先處理後段 prompt，再處理前段 RX。

Mailbox 必須防止 RX flood 餓死停止、取消、Lease 到期與 Drain 回條。Slice 2.1 開始前，由 PAL／Actor 實作負責人與獨立測試審閱者，凍結各事件從可觀測受理點至 Actor 處理完成的最大延遲、測試負載及量測方法；L1 驗證可控排程，L1.5 驗證外部可觀測上限。事件處理延遲與 3,000 ms Drain 完成時限分別判定，不得互相代用。

| 狀態／事件 | 必要 guard 與副作用 | 結果 |
|---|---|---|
| `DETACHED`＋有效裝置出現 | 通過裝置選取與獨占；建立新 worker | `ATTACHING` |
| `DETACHED`／`ATTACHING`＋一般命令 | 尚未具命令能力 | `SESSION_NOT_READY` |
| 活躍態＋BootBannerDetected | 核對連線；啟動 Quiet、更新 `fsm_gen`、設 Reconfirm pending、失效舊 DeferredBuffer | 保持對外基本狀態，掛載 Gate |
| Quiet／Reconfirm pending＋一般命令／FileTransfer | 拒絕入列或拒絕尚未開始的寫入 | `AUTOBOOT_QUIET` 與 `retry_after_s` |
| Ready Reconfirm 耗盡 | 逾既有 300 秒或滿 5 次失敗；清 pending、設 failed | 後續一般操作回 `READY_UNCONFIRMED` |
| 有效 ReadyProbeVerified | epoch、`fsm_gen`、nonce 相符 | `READY`，解除重驗 Gate |
| 登入帳密未解析 | 設 session `last_error=CREDENTIALS_UNRESOLVED`；停止自動重探；依既有行為一次性 log＋WAL 警告 | 不新增 SessionState；實際基本狀態依基線凍結 |
| 補帳密後明確 attach／recover／重啟 | 重新解析帳密並啟動核准的登入流程；仍受 Quiet 與交接安全規則限制 | 依登入結果前進 |
| `ATTACHED`＋救援 InteractiveOpen | `allow_attached` 且符合既有 bootloader／banner 條件，且無衝突 | 建立救援 Lease，回 `boot_interrupt` |
| `ATTACHED`＋不合格 InteractiveOpen | 不符合救援條件 | `SESSION_NOT_READY` |
| 非 passthrough `ATTACHED`＋CmdSubmit | 無一般命令能力 | `SESSION_NOT_READY` |
| passthrough `ATTACHED`＋CmdSubmit | 非 Quiet、非失效、無衝突 Lease、容量足夠 | 進入 Arbiter |
| `READY`＋InteractiveOpen | 無 Agent Lease；Human 不活躍時允許 Soft Preempt | 授予 Lease，必要時回 `soft_preempted` |
| `READY`＋InteractiveOpen、已有衝突 | Human 活躍或已有 Agent Lease | `SESSION_INTERACTIVE_BUSY` |
| 有效 InteractiveSend／Status | 核對 Lease 及目前狀態；依既有契約 touch／延長 | Send 仍經受控 writer；Status 不送 UART |
| 無效／過期 Interactive 操作 | 核對實際存在與到期狀態 | `INTERACTIVE_NOT_FOUND`／`INTERACTIVE_EXPIRED` |
| InteractiveClose／Expired | 核對 Lease；到期事件須核對目前 deadline，不能使用 touch 前的舊期限 | 結束 Lease；只有上下文仍有效才還原 Human／回放 |
| `READY`＋CmdSubmit | 非 Quiet／Unconfirmed、無衝突、pending 小於 256 | Admission；執行前再次檢查 |
| 命令容量滿 | Accepted／Running 已達上限 | `SESSION_QUEUE_FULL` |
| Console 輸入 | 核對 client 生命週期、owner 與連線上下文 | 有授權則送 writer；suspend 則緩衝；失效則丟棄並可診斷 |
| FlashSyncProbeStart | 平台能力存在，候選埠符合凍結的認線資格，且不處於 RELEASED、RECOVERING 或未完成的 Drain；須確認不存在與 probe 授權衝突的執行中操作，並遵守 §5.2.1 不得與一般命令並行的限制。操作身分與授權仍有效，並經同一受控 writer 取得 probe 授權。Probe 不得自行重新開啟已 released 的裝置，也不得繞過尚未完成的 handle 交接。 | 凍結一般 TX，持有 probe 授權 |
| FlashSyncMatched | `probe_id` 與 ACK 相符 | `FLASHING`，清理 DeferredBuffer，專用橋接 |
| Probe 釋放／未匹配 | 僅解除同一 probe 的授權 | 恢復有效上下文，不回放已失效資料 |
| `FLASHING`＋CmdSubmit／DeviceRelease／InteractiveOpen | 不允許搶占 Flash | `FLASHING_BUSY` |
| FlashHolderGone | 確認對應 holder 已離開，完成模式／連線交接 | 回 `ATTACHED`，記錄完成事件 |
| 活躍態＋斷線／I/O error／recover | 核對事件來源；啟動 Drain；停止新授權 | `RECOVERING` |
| Recovery 清除未執行命令 | 可確認尚未提交 OS write | `FLUSHED_BY_RECOVERY` |
| Recovery 中止已確認部分 TX | 已確認寫出至少 1 byte | 對應 artifact 完成 BC-03 實作及契約驗證後，回 `COMMAND_ABORTED_IN_FLIGHT`，不可自動重送 |
| Recovery 中止結果不明 | 不能证明零 TX | 保留不確定性；不可自動重送；依凍結映射回報 |
| `RECOVERING`＋CmdSubmit／InteractiveOpen | 尚未恢復 | `SESSION_RECOVERING` |
| 非 `FLASHING`＋DeviceRelease | 停止新 TX、Drain、關閉、清理上下文、持久化 released | 完成後才進 `RELEASED` 並回成功 |
| `RELEASED`＋自動裝置出現 | released 仍有效 | No-op，不自動 attach |
| `RELEASED`＋CmdSubmit／InteractiveOpen | 尚未明確收回 | `SESSION_NOT_READY` |
| `RELEASED`＋DeviceAttach | 重新取得獨占並開啟成功 | `ATTACHING`；失敗不得宣稱已收回 |
| Drain timeout | worker 尚未確認停止 | 保持 `RECOVERING`、標示 stalled |
| 過期 timer／ownership 事件 | 不符合目前 operation／generation | 不改變目前狀態、不觸發新 TX |
| 過期 I/O completion | 屬舊 worker | 保存可確認的 I/O 證據，不套用新狀態 |

表中未逐項列出的既有 query、cancel、capture、file 與管理命令，仍依第 9 章契約及案例承接清單處理；不得以未列於本表為由回覆無條件成功。

尚未擷取的精確欄位值或錯誤優先序標為「待基線擷取」，不是留給實作者自由選擇。切片出口前須完成其必要範圍的凍結。

### 5.4 背壓與有界資源

| 資源 | 上限／處置 |
|---|---|
| `CMD_PENDING_MAX` | 每 Session 256 筆 Accepted／Running；滿時拒絕 Admission，回 `SESSION_QUEUE_FULL` |
| `CMD_HISTORY_MAX` | 512 筆；淘汰最舊已完成紀錄，不淘汰仍在途命令以隱藏負載 |
| `BG_CAPTURE_MAX_COUNT` | 64 個；依既有策略淘汰已結束 capture |
| `DEFERRED_INPUT_MAX_BYTES` | 262,144 bytes；超過上限依 BC-02 整段清空，不截頭留尾 |
| EventEngine LineBuffer | 16,384 bytes；超過上限依既有強制切行規則處理 |
| 每 Console 出向 Ring Queue | 1 MiB；慢速 Console 可 Drop-Oldest，不得阻塞 UART／WAL／其他 Console |
| Actor mailbox、RX／TX 待處理資料、WAL 待處理資料 | 必須有界；精確容量及滿載策略於 Slice 2.1 前凍結，不得使用無界 channel 當作實作預設 |
| bridge 證據／轉接緩衝 | 依第 15 章；錄證失效不得靜默丟棄後判 PASS |

Console 顯示資料的允許丟棄，不得擴張成 WAL、命令 TX、裝置 RX 證據或控制事件可靜默丟棄。

WAL 無法持續記錄時須進入可診斷的有界失敗處置，停止新增自動 TX；不得在記憶體無限堆積，亦不得把未記錄資料當成已完成證據。

L1.5 的同板多來源競爭案例不得使用測試端 per-board lock 替 broker 序列化。`lr-mixed` 則保留既有 per-board lock，以維持長跑基線可比性。

### 5.5 Event Engine 行切分（`LineBuffer`）與外部子程序模型

* 
每個受管 COM 埠配備一個獨立 `LineBuffer`（上限 `16 KiB`），將 `dir == "RX"` 切分為行並剝除 ANSI Escape Sequences 後，才執行 Regex 比對。

* 
完整保留規則 Schema 之 `cooldown_ms` 與 `max_fires`。

* 
命中時由 `Dispatcher` 執行緒池以 `subprocess` 執行規則定義之 `exec` 列表或 `shell` 字串；子程序本身不直接寫 UART，若需下命令則透過外部 CLI 呼叫 `serialwrap cmd submit`。

## 6. 監管模式設計與雙層互斥鎖協定

### 6.1 設定權威與監管模式

`config.yaml` 的 `supervision_mode` 維持單一事實來源。設定發現順序為明確指定的 config／`SERIALWRAP_CONFIG`、使用者設定目錄、系統設定目錄；實際路徑解析依 PAL 與凍結契約執行。

| 平台 | 模式 | 條件 |
|---|---|---|
| Linux | `systemd-system`、`systemd-user`、`on-demand` | 驗證實際 unit、權限、啟停與唯一實例，不只檢查產生的文字 |
| Windows | `windows-task` | 測試團隊工作站預設（OD-03 核准）；登入使用者身分、登入觸發、失敗重啟 |
| Windows | `windows-service` | 僅於 OD-12 信任模型符合時採用；帳號、ACL 與 Session 0 行為須實測，不預設必须使用 LocalSystem |
| Windows | `on-demand` | 首次需求啟動；仍受實例鎖、Per-UART 鎖與版本配對約束 |

採 `windows-task` 時設定 `ExecutionTimeLimit=PT0S`，並分別驗證設定、失敗重啟及實際運行超過 72 小時。Task Scheduler 的預設 72 小時限制與 `PT0S` 無限期語意，不可只用 48 小時長跑間接判定。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit)

L3 故障保留模式與監管重啟測試分開執行。`lr-mixed` 的隔離測試實例不得因外部監管器自動重啟而消除 daemon death 證據。生產 Canary 可啟用監管，但非預期重啟仍計為事故。

所有 reconcile、更新及回滾均須避免舊監管項與新監管項同時拉起不同版本。

### 6.2 雙層互斥、錯誤分類與保證範圍

| 檢查 | 目的 | 規範 |
|---|---|---|
| Tier 1 實例鎖 | 防止同一實例資源被重複使用 | POSIX 檔案鎖／Windows `LockFileEx`；路徑須正規化；回 `DAEMON_ALREADY_RUNNING` |
| CLI 跨 socket 預檢 | 防止誤啟另一個 daemon | 顯示已發現的 PID／endpoint；回 `DAEMON_ALREADY_RUNNING_ELSEWHERE`；`--force-spawn` 只放行此檢查 |
| Tier 2 UART | 保護受管裝置 | 正常 serial backend 取得獨占；失敗時不啟動一般收送 |
| Bench 排程鎖 | 協調 regression、reliability 與其他實機工作 | 不取代 Tier 2；跨主機共用 bench 時另需共同租用／排程機制 |

#### POSIX

對已開啟的 tty fd 取得非阻塞 `flock` 並設 `TIOCEXCL`，加上必要 holder 診斷。`flock` 是 advisory lock；`TIOCEXCL` 阻止後續一般開啟，但不撤銷既有 handle，且存在 `CAP_SYS_ADMIN` 例外。[man7.org](https://man7.org/linux/man-pages/man2/flock.2.html)

因此，BC-04 的保證限定為：在受控權限、指定裝置存取流程與已驗證的驅動／裝置組合下，避免 serialwrap 實例互相搶讀，並拒絕已識別的外部衝突。

`/proc` holder 掃描是診斷快照，不是原子鎖。查不到 PID 不代表不存在 holder；權限不足或掃描失敗不得解讀為安全證明。

必測情境包含外部程式先開埠、兩個 daemon 競爭開埠、symlink 指向同一裝置、程序退出、handle 繼承及受限 `/proc`。需要更強保證的 bench 應限制裝置權限，要求外部工具遵守 release／attach 流程。

#### Windows

使用正常 COM handle 的 `CreateFileW`、`OPEN_EXISTING`、`dwShareMode=0`。這是 Windows 通訊資源的原生獨占開啟路徑。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/devio/communications-resource-handles)

已確認占用時使用 `DEVICE_STILL_HELD`。無法反查 holder 時回 `pids: []`、`holder_unknown: true`。

`ERROR_ACCESS_DENIED` 本身不能證明具體占用者；也可能缺少足夠資訊判定原因。此時保留 OS 錯誤與不確定性，依凍結的相容映射處理，不虛構 PID 或確定原因。需要新增診斷欄位時由 OD-04／OD-13 核准。

#### 交接

`device release` 成功須同時滿足：新 TX 已停止、在途 I/O 已收斂、daemon handle 已關閉，且 released 狀態已依凍結的持久化契約提交，能在 daemon 重啟後維持禁止自動 attach。提交失敗時不得回覆成功，當前 daemon 須維持禁止自動 TX 與自動 attach，並提供失敗診斷。程序中斷測試須區分持久化提交完成前後：提交完成後，即使成功回覆尚未送達，重啟仍須保持 released；提交完成前則依最後有效的持久化狀態恢復，不得宣稱本次 release 已完成。無法確認有效持久化狀態時，須維持阻擋並提供診斷，不得自行假定可 attach。

外部工具完成後，由明確的 `device attach` 重新取得裝置。鎖取得失敗時不得清除保護後繼續操作。

虛擬 COM allowlist、sandbox 或 `--force-spawn` 均不得繞過本節獨占與交接。

## 7. Console 後端與虛擬序列埠角色

### 7.1 POSIX PTY

保留既有 PTY Console、穩定 symlink 與必要 keepalive 行為。Console PTY 與作為 DUT 的測試 PTY 必須有不同角色及身分，不得把 Console 端點重新列舉成受管 UART。

`/dev/ttyMCU` 的常駐端點、認線與 holder-probe 依適用平台保留，完整承接於 Slice 2.4。

### 7.2 TCP＋Telnet

Windows 基準 Console 監聽 `127.0.0.1`。保留：

| 方向 | 行為 |
|---|---|
| Daemon→Client | 既有 `WILL ECHO`、`WILL SGA`、`WILL BINARY`；`0xFF` 逸出 |
| Client→Daemon | 維持既有 NVT 語意；拒絕入向 Binary 協商；跨 chunk 摺疊 `CR LF`／`CR NUL` 為 `CR`；`0xFF 0xFF` 還原 |

協商 parser 必須處理跨 chunk、未完成序列、重複協商及多 Console 狀態，不能將 Telnet 控制 bytes 當成 DUT TX。

第一順位 Raw、後續 Line-buffered、Soft Preempt、Suspend／Resume 與 DeferredBuffer 依第 5 章驗證。Telnet 可達範圍不等於使用者認證，部署受 ADR-04 約束。

### 7.3 Console-side 虛擬 COM

依 OD-01 為選配，僅在驅動可部署的機台啟用。啟用時，一端由 daemon Console backend 開啟，另一端供 Tera Term Serial 使用；Telnet 仍可提供其他觀察者連線。

虛擬埠的 DTR／RTS／baud 設定不得直接轉送到實體 DUT UART。需使用虛擬端狀態判斷連線時，亦須明列其用途與測試結果。

驅動資格須涵蓋指定 Windows build、Secure Boot、HVCI 與實際功能；僅有簽章不足以取代 memory integrity 啟用環境下的功能測試。[Microsoft Learn](https://learn.microsoft.com/en-us/windows-hardware/test/hlk/testref/driver-compatibility-with-device-guard)

### 7.4 DUT-side 測試 COM

L1.5 使用另一組端點：serialwrap 的原生 serial backend 開啟 COM-A，外部 bridge 開啟配對 COM-B。

兩組 pair 不可混用。一般裝置發現仍排除 Console-side com0com；OD-11 可核准僅作用於隔離實例的明確 DUT-side allowlist。

Allowlist 只能調整裝置發現／選取，不能替換 serial backend、停用鎖或修改仲裁。

關閉 COM-B 不自動等於 COM-A 被 PnP 移除。每種 fault action 須依實測能力命名；不能把停止對端讀寫算成 USB 拔插。

### 7.5 OD-01 與 OD-08 的分工

OD-01 決定產品是否必須提供 Serial Console；OD-08 決定如何取得 Windows 原生 serial backend 的測試證據。

Serial Console 為選配，不代表 Windows Serial 測試為選配。com0com 不符合部署政策時，可以改用核准 driver 或實體 bench，但不能以記憶體 fake 替代後標示原生 serial gate 已通過。

## 8. WAL 證據層級、常駐開檔、Pending-Delete 自癒與持久化設計

### 8.1 WAL Schema 與證據語意

保留 `raw.wal.ndjson` 頂層欄位：

| 欄位 | 型別 |
|---|---|
| `seq` | `u64` |
| `mono_ts_ns` | `u64` |
| `wall_ts` | ISO-8601 string |
| `com` | string |
| `dir` | `RX`／`TX`／`META` |
| `source` | string |
| `cmd_id` | string 或 null |
| `len` | `u64` |
| `crc32` | `u32` |
| `payload_b64` | string |
| `loss_flag` | bool |
| `meta` | object |

`len`、CRC 與 payload 必須相符。CRC 變體、META 空 payload、seq 在 rotate／reset／daemon restart 的行為，須由 Stage 0 擷取並凍結，不由 Rust 實作者自行推測。

#### RX

正常路徑在 OS read 成功後，先完成 WAL append／flush，再向 Console、EventEngine 與 Login FSM 提供可處理資料。

此順序不保證「OS 已讀取、但程序在 append 前 crash」的資料一定存在於 WAL。故障時須依實際可取得的證據描述缺口。

#### TX

只為已確認完成的 OS 寫入 bytes 建立 TX 紀錄；overlapped I/O 的提交或 `ERROR_IO_PENDING` 不等於完成。確認長度小於要求長度時，只記錄確認部分。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile)

TX WAL 不證明 DUT 收到或執行命令。DUT-side bridge 的對端讀取與 DUT 回覆，分別提供更外層的證據。

#### Flush 與持久化

每筆 flush 用於提交應用程式緩衝內容，不等同每筆 durable sync。正常 rotate／優雅關機依 PAL 執行檔案同步；POSIX 的目錄同步與 Windows 的對應操作須分別驗證，不能宣稱完全相同。Rust `File::sync_all` 與一般 `Write::flush` 的責任亦須區分。[Rust 文件馆](https://doc.rust-lang.org/std/io/trait.Write.html)

UART write、WAL append 與磁碟持久化不存在跨裝置原子性。因此禁止宣稱「所有已送出 bytes 在任何 daemon crash 後必有 WAL」或「可由 WAL 保證非冪等命令安全重送」。

### 8.2 常駐開檔、Rotate／Reset 與外部刪檔自癒

本節屬 BC-01，已依 OD-04 核准（`APPROVED`，2026-10-15）。

#### 常駐開檔

`raw.wal.ndjson` 與 `raw.mirror.log` 常駐開啟，每筆依既定順序完成寫入及 flush。Windows 由 PAL 明確取得包含 `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` 的檔案 handle。

Python 實作可採 `CreateFileW`、`open_osfhandle` 與檔案物件包裝，但必須驗證 handle 所有權、失敗清理及唯一 close；不得發生雙重關閉或例外路徑洩漏。

#### 正常 Rotate／Reset

同一 writer 序列化停止新紀錄插入、完成規定的 flush／sync、關閉自身 handle、執行 rename／reset、重開及繼續記錄。

正常 rotate 保留可追蹤的檔案與 seq 關係。不得因同名新檔出現，就讓 F12 或比較器把舊檔當成目前權威來源。

`raw.mirror.log` 不自動等同可重建完整 WAL 的備份；是否可恢復資料以實際格式與驗證結果為準。

#### 外部 unlink／替換偵測

POSIX 比對路徑與 fd 的檔案 identity；Windows 檢查適用的 `DeletePending` 與檔案 identity。50 ms 是 v4 提議的檢查節流間隔，不是可保證的最大資料遺失窗口。

自癒流程：

1. 標記檔案關聯失效，停止把旧 handle 當成目前路徑的有效證據。
2. 保存可得的檔案身分、seq 範圍與錯誤資訊；關閉 daemon 自身相關 handle。
3. 以有界重試重新建立或開啟所需檔案。
4. 成功後寫入首筆 META，再恢復正常紀錄。

傳統 Windows pending-delete 語意下，刪除可能等到最後一個 handle 關閉才完成；外部 reader 仍持有 handle 時，關閉 daemon 自身 handle 不保證可立即重建同名檔。不同刪除模式亦須分別實測。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-deletefilew)

不得強制關閉其他程序 handle、無限重試，或將重建失敗吞掉後繼續宣稱 WAL 健康。

#### 遺失範圍

重建事件保留：

`WAL_RECREATED_AFTER_EXTERNAL_UNLINK`、`loss_flag: true`、`potentially_lost_seq_range`。

潛在遺失範圍須考慮**整個被刪實體中無法由保留檔案恢復的紀錄**，不能只填最後 50 ms。若無法取得確切範圍，依核准 META schema 明確表示未知，禁止填入猜測的起訖。

BC-01 已核准 META 的缺口記錄範圍；未知範圍表示、identity、原因欄位及其型別，須於 Stage 0 的 BC-01 契約測試執行前具體化並記錄契約 revision。不得僅以 BC-01 已核准宣稱 schema 已完成，也不得私自更動頂層 WAL schema；超出核准範圍者須新增裁決。

#### 記錄不可用時

停止新增自動 TX，維持有界資源與可用診斷，保留 I/O 結果不確定性。已完成 UART 寫入不能因 WAL append 失敗而自動重送。


### 8.3 F12 WAL 活性 Oracle 與外部比對

既有 F12 以 `daemon status.wal_path` 在送命令前後的 mtime 判斷活性。改為常駐 handle 後，Windows last-write time 的更新時機不足以支撐此 oracle；Windows 文件明載，寫入 handle 關閉前 last-write time 可能尚未完整更新。[Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile)

F12 改採以下程序：

| 步驟 | 驗證 |
|---|---|
| 起始 | 取得 status 的 `wal_path`，確認測試機可實際讀取，記錄檔案 identity 與最後完整紀錄 |
| 動作 | 送出具有唯一測試 marker、來源或可關聯 `cmd_id` 的安全命令 |
| 等待 | 在有界期限內讀取新增完整紀錄；背景雜訊造成的 seq 前進不足以證明該命令已記錄 |
| 判定 | 找到與該動作可關聯的新增 TX／RX 紀錄，驗證 JSON、payload、長度與 CRC，並核對凍結的 seq 語意 |
| Rotate／Reset | 重新辨識目前路徑與保留檔案；依已凍結的生命週期繼續查找，不要求新檔大小一定大於舊檔 |
| 結束 | 保存讀到的原始紀錄與判定依據；timeout、無法讀取或證據不完整均不得 PASS |

檔案大小、mtime 可保留為診斷，不是唯一成功條件。讀取器的 share mode 須符合測試目的；一般活性案例不得無意阻擋產品 rotate，刻意測外部 reader 阻擋則另列案例。

WAL range 的 `truncated=true` 表示仍有下一頁，不代表遺失；比較器須依現有分頁契約讀完必要範圍。

此修訂屬 OD-13 的 oracle 核准項目，不是為配合 Rust 結果降低要求。

## 9. 契約凍結、行為變更與診斷命名空間

### 9.1 行為變更提案：BC-01～BC-05

下列項目已於 2026-10-15 依 OD-04 全數核准為 **`APPROVED`**。核准後先在 Python 基線實作並取得對應證據，再作為 Rust 承接基準。

| 編號 | 提議變更 | 核准範圍與必要驗證 |
|---|---|---|
| BC-01 | WAL 常駐 handle、每筆 flush、Windows share-delete、自癒與缺口記錄 | 同時核准正常 rotate、外部 handle 阻擋、有界失敗處置及 META 語意；F12 oracle 由 OD-13 配套核准 |
| BC-02 | DeferredBuffer 溢位或跨上下文時整段清空 | 保留未發生失效時的 #53 正常回放；測 mailbox 中尚未處理的舊輸入，避免清空後重新灌回 |
| BC-03 | 新增 `COMMAND_ABORTED_IN_FLIGHT` | 已確認寫出至少 1 byte 後中止，`retryable:false`；結果不明須另凍結相容映射，不得混入安全重試 |
| BC-04 | Per-UART 獨占、衝突阻斷與 holder 診斷 | 明列 POSIX 保證範圍、Windows `pids:[]`／`holder_unknown:true`；占用與原因不明的存取失敗不得誤表述 |
| BC-05 | Windows `remote -R/-L` | 移除 POSIX-only pipe 依賴；驗證非阻塞轉接、程序生命週期、停止、重啟及既有三拓樸 |

每項核准紀錄至少包含擁有者、日期、適用版本、契約 revision、案例 ID、相容性影響與核准條件。

若未核准的變更是某階段安全出口的必要前提，該出口保持阻擋；不得以「尚未核准」為由跳過安全機制後交付。

### 9.2 表一：RPC／CLI `error_code` 權威集合

`contracts/error_codes.yaml` 管理 RPC／CLI 回應中的 `error_code`。其中部分錯誤由本地 CLI 產生，不能因此宣稱全部均為 daemon RPC server 錯誤；每個 entry 須標記實際適用介面。

本版候選集合為 **71 個既有字面碼＋6 個既有常數碼＋1 個新增已核准碼＝78 個不同碼**，不代表全部已在指定 artifact 生效。BC-03 已核准，但在對應實作與契約測試完成前，新增碼不得被描述為已生效的既有契約。

| 分類 | 完整代碼集合 | 狀態 |
|---|---|---|
| 71 個既有字面碼 | `ALIAS_NOT_FOUND`、`BASE64_DECODE_FAILED`、`BENCH_STATE_IO_ERROR`、`CHECKSUM_MISMATCH`、`CHECKSUM_VERIFY_FAILED`、`CMD_NOT_CANCELABLE`、`CMD_NOT_FOUND`、`CONSOLE_LINE_LIMIT_TOO_SMALL`、`CONSOLE_NOT_FOUND`、`DAEMON_ALREADY_RUNNING_ELSEWHERE`、`DAEMON_BINARY_NOT_FOUND`、`DAEMON_EXITED`、`DAEMON_NOT_READY`、`DEVICE_ALREADY_BOUND`、`DEVICE_NOT_BOUND`、`DEVICE_NOT_FOUND`、`DEVICE_NOT_PRESENT`、`DEVICE_STILL_HELD`、`ECHO_ACK_UNSUPPORTED`、`EMPTY_RESPONSE`、`ENV_FILE_SOURCE_FAILED`、`EXCEPTION`、`FLASHING_BUSY`、`FLASH_TARGET_IS_CONSOLE`、`FLUSHED_BY_SHUTDOWN`、`FORCE_RECOVER_TIMEOUT`、`INTERACTIVE_EXPIRED`、`INTERACTIVE_NOT_FOUND`、`INTERNAL_ERROR`、`INVALID_ARGS`、`INVALID_BENCH_STATE`、`INVALID_ENDPOINT`、`INVALID_JSON`、`INVALID_METHOD`、`INVALID_REQUEST`、`INVALID_RESPONSE`、`INVALID_RULE_SCHEMA`、`LOCAL_FILE_NOT_FOUND`、`LOG_OPEN_FAILED`、`METHOD_NOT_FOUND`、`MOVE_FAILED`、`MOVE_TIMEOUT`、`NEEDS_SUDO`、`NO_ACTIVE_CAPTURE`、`NO_SYSTEMD`、`PROFILE_IS_EXPLICIT`、`PROFILE_NOT_COMMAND_CAPABLE`、`PROMPT_TIMEOUT`、`PROMPT_TIMEOUT_FORCE_RECOVERED`、`PROMPT_TIMEOUT_RECOVERED`、`PULL_PARSE_FAILED`、`REMOTE_NOT_SUPPORTED`、`RULE_NOT_FOUND`、`SAMPLE_READ_FAILED`、`SESSION_BUSY`、`SESSION_INTERACTIVE_BUSY`、`SESSION_NOT_FOUND`、`SESSION_NOT_READY`、`SESSION_QUEUE_FULL`、`SESSION_RECOVERING`、`SOCKET_ERROR`、`STATE_CHANGED`、`TARGET_DECODER_FAILED`、`TARGET_ENCODER_FAILED`、`TIMEOUT`、`TRANSFER_ECHO_STALL`、`TRANSFER_TIMEOUT`、`TUNNEL_STILL_ACTIVE`、`UNKNOWN_EVENT_CMD`、`UNKNOWN_PROFILE`、`WAL_MISSING` | `EXISTING` |
| 6 個既有常數碼 | `DAEMON_ALREADY_RUNNING`、`FLUSHED_BY_RECOVERY`、`AUTOBOOT_QUIET`、`READY_UNCONFIRMED`、`CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE` | `EXISTING` |
| BC-03 新增碼 | `COMMAND_ABORTED_IN_FLIGHT` | `APPROVED`（OD-04，2026-10-15）；實作前尚未生效 |

每個生效 entry 必須具有非空 `meaning`、`side_effect`、`retryable`、`agent_action`、適用介面與對應契約測試。

計數由目錄內容產生，政策不得硬編碼「永遠 78 個」。**依 OD-06**，說明覆蓋規則置於 paulsha-conventions，為**通用規則、不內建任何錯誤碼清單**：凡程式會輸出的 error code（以及 session `last_error` 值），都必須在 help／SKILL 具有意義與處置說明。各 repo 只以設定宣告「如何抽出會輸出的 code（含常數傳遞路徑）」與「說明文件位置」；上表僅為 2026-10 的現況盤點，不得成為規則或政策的固定清單。掃描須包含常數傳遞與回應建構路徑，不能只 grep 字面字串。

不得為了提高覆蓋率，將所有未分類錯誤改成 `INTERNAL_ERROR` 而丟失既有外部契約。

### 9.3 表二：Session `last_error` 值

`contracts/session_last_errors.yaml` 管理 Session 診斷欄位，不與 RPC／CLI `error_code` 共用計數或列舉型別。

| 值 | 欄位與來源 | 終態／自動行為 | 恢復方式 | 證據與測試 |
|---|---|---|---|---|
| `CREDENTIALS_UNRESOLVED` | session 的 `last_error`；由 `login_fsm` 回傳，`session_manager` 進入相應終態時設定 | 登入終態，不自動重探；不得對 login prompt 送空帳密 | 補齊帳密後，由使用者明確執行 `session attach`、`recover`，或重啟 daemon 重新進入流程 | 進入該終態時一次性 log＋WAL 警告；外部驗證未送空帳密、等待不會自動重探，並驗證手動恢復 |

本表的「一次性」指一次進入該終態的處理，不代表 daemon 永久只能記錄一次；再次經明確恢復後重新遇到同一終態，依既有凍結行為處理。

`CREDENTIALS_UNRESOLVED`：

- 不新增為 RPC `error_code`。
- 不新增為 SessionState。
- 不因使用者只修改帳密檔案，就自動解除終態並送 probe。
- 不允許由 Agent 直接改寫 `state.json` 或內部 `last_error` 假裝已恢復。

其他既有 `last_error` 值的完整集合及清除時機尚待 Stage 0 基線擷取；本表不宣稱此值是唯一可能值。其實際基本 SessionState、null／空值表示亦須擷取，不以推測補入。

### 9.4 L2／L3 依賴欄位與外部契約

| 契約面 | 必須凍結的內容 |
|---|---|
| CLI | 20 群組、全域選項、stdout／stderr、exit code、JSON 格式與 11 方法重試白名單 |
| 版本 | `daemon status.version`、CLI `--version`、兩者配對規則與拒跑行為 |
| WAL 診斷 | `wal_path`、`doctor.wal_dir`、可讀性及 §8.3 實際進展 oracle |
| 占用 | `multi_open`、`foreign_holders`、`DEVICE_STILL_HELD`、`pids`、`holder_unknown` |
| 活動 | `rx_bytes_last_10s`、`last_rx_age_s`、`last_tx_age_s`、`last_error_detail`；單位、更新與未有資料時表示 |
| Self-test | `classification`；含 `RX_FLOOD`、`BOOTLOADER`、`AUTOBOOT_QUIET` |
| Profile | `profile_bootloader_prompts` 檢查項及適用條件 |
| Boot／Ready | `ready_reconfirm_pending`、`ready_reconfirm_failed`、`retry_after_s`、`recommended_action` |
| Session 診斷 | `last_error` 與 §9.3 的獨立值集合 |
| 命令／容量 | `limits`、命令結果／取消／tail、排隊與在途錯誤的分界 |
| Drain | `drain_stalled`、`drain_timeout_epoch` 及 `FORCE_RECOVER_TIMEOUT` 的適用語意 |
| WAL 查詢 | range、分頁、`truncated=true` 與 seq／檔案生命週期 |
| 持久化 | `state.json` 欄位、released 狀態、升降版相容與備份還原 |
| 測試裝置選取 | OD-11 已核准的隔離實例 allowlist；具體設定名稱、輸入位置、解析優先序、隔離實例判定、正式設定拒絕方式及 status 顯示欄位，須在 Stage 0 首次執行相關黑箱案例前納入契約並凍結，不能以 OD-11 的範圍核准代替具體契約。 |

「凍結」包含型別、required／optional、null／缺省、單位、enum、更新時機、副作用及錯誤優先序。不得只列欄位名稱而讓實作者自行猜測。

必要黑箱案例至少保存 JSON、exit code、外部 TX／RX、WAL 與狀態軌跡。現有 Python 結果違反已核准安全不變量時，不作為必須複製的正確答案。

### 9.5 發佈組合版本與 artifact 身分

Stage 1 Rust CLI＋Python daemon 使用同一發佈組合版本，並由 release manifest 分別記錄 component implementation、Git revision、artifact digest 與契約 revision；組合版本必須對應實際配對的元件，不取代各元件身分。

此版本策略已依 OD-13 核准；不得透過任意改寫 `--version` 或 status 回應，偽造不存在的配對關係。

既有版本閘門維持：pinned CLI 與 daemon 版本不一致時，L2／L3 整場拒跑。若將來改採相容版本矩陣，屬另一次契約變更，不得直接放寬目前 gate。

`binary_sha256` 如對外提供，須明確指向哪個實際執行檔。Python daemon 若以 interpreter 執行，單獨的 `python.exe` hash 不代表所有受測 Python 程式碼；release manifest 必須另提供部署程式碼／套件的 artifact digest。

測試報告須同時記錄 CLI、daemon、測試工具、DUT image、案例與設定身分。

## 10. AI Agent 治理與驗收完整性

### 10.1 目錄與生成

政策引擎分別驗證：

| 命名空間 | 權威資料 | 必要內容 |
|---|---|---|
| RPC／CLI `error_code` | `contracts/error_codes.yaml` | meaning、side effect、retryable、agent action、scope、契約測試 |
| Session `last_error` | `contracts/session_last_errors.yaml` | 意義、終態性、自動行為、手動恢復與記錄規則 |
| 行為變更 | `contracts/behavior_changes.yaml` | 狀態、擁有者核准、適用版本、相容性影響與案例 |
| 測試工具原因碼 | 測試工具自己的 schema | 與產品命名空間分離，不算進產品 78 碼集合 |

依 OD-06，於 paulsha-conventions 新增通用規則（必要時搭配 R-25／R-26），生成 `SKILL.md` 的受管區段。工具須核對實際回應位置與常數傳遞，避免把所有大寫字串混成錯誤碼。

### 10.2 排障流程

Agent 遇錯時先查相應命名空間的處置，執行必要的唯讀診斷，依規範取得遮罩後證據並開立 Issue。

收到 `CREDENTIALS_UNRESOLVED` 時，應處理帳密來源並明確重新啟動登入流程，不反覆自動 probe、不送空帳密、不直接清除狀態。

具有或可能具有硬體副作用的命令，不因 timeout、取消或證據不足自動重送。`session interactive-status` 會 touch Lease，不得當成無副作用監控或加入自動重試白名單。

### 10.3 部署與修改權限

測試工作站原則上部署 release artifact 與操作文件，不提供可讓一般任務 Agent 就地修改的產品工作樹。Stage 1B 前須由部署負責人明列一般任務 Agent 與安裝／更新者的執行身分及有效權限，並以實際 Agent 權限驗證其不能覆寫、刪除、重新命名或替換版本檔案、穩定入口及相關父目錄。更新權限須由一般任務 Agent 無法取得的受控機制持有；若部署仍讓兩者共用相同替換權限，只能宣稱流程約束，不得宣告已完成權限阻斷目標。

禁止 Agent 為完成其他工作而修改 serialwrap、停用 Gate、變更測試 oracle、直接開受管 UART，或將受測物回報當成唯一成功證據。

### 10.4 測試工具變更

修改 bridge、DUT、比較器、契約或哨兵的 PR 必須有獨立審閱。驗收使用核准的工具 revision；產品與 oracle 同時修改時，須分別呈現變更理由及既有基線對照。

不得透過刪案例、改分母、取消必要能力、放寬正規化或把 FAIL 改 SKIP 取得全綠。

## 11. 測試與 CI 策略

### 11.1 測試金字塔與證據邊界

本專案採用四層測試金字塔。各層驗證不同性質的不變量，不得以某一層通過，取代其他層尚未取得的證據。

| 層級 | 套件與執行介面 | 能證明的範圍 | 不能據此證明的範圍 |
|---|---|---|---|
| **L1：決定性邏輯與契約** | 既有 `tests/` pytest、Rust 單元測試、`tests/contract_blackbox/` 的無實體串口案例 | 契約格式、錯誤碼、狀態機、仲裁與容量限制、可控時鐘下的競態、CLI subprocess 行為 | 真實序列埠驅動、Windows COM handle、實體 UART 訊號與長時間穩定性 |
| **L1.5：外部序列埠端到端** | `func-test/` 擴充外部 target 模式；`uart-stdio-bridge`＋獨立 DUT 模擬器；使用虛擬配對或 OD-08 核准的實體傳輸替代 | 真正 CLI／RPC／daemon／serial backend 經指定作業系統端點的行為；serialwrap 外部可觀測的 TX／RX bytes；Python→Rust 差分；報告須區分虛擬與實體傳輸模式 | 虛擬模式不證明實體 UART 電氣特性及真實 USB-UART 韌體／驅動行為；實體替代模式僅涵蓋已測硬體、驅動與情境，不取代真實 DUT 的 L2 回歸或 L3 長跑 |
| **L2：實機回歸** | `regression/`，TestPilot plugin `serialwrap_regression`；既有 12 family、37 case | 指定板子、韌體、序列埠與主機組合上的既有 bug 回歸、裝置交接、登入、Console 共存及指定硬體流程 | 未測硬體組合、所有競態排列、48 小時以上的可靠度 |
| **L3：實機穩定性與長跑** | `realhw/` 的 P0、P1、remote、longrun；TestPilot plugin `serialwrap_reliability` 為同引擎第二前端 | 指定負載與環境下的長時間存活、卡態、命令完成率、資源趨勢及故障處置 | 任意環境下永不故障；未執行的硬體流程；被測試端序列化掉的 broker 競爭情境 |

測試層級由實際執行路徑決定，不由目錄名稱決定。例如，放在 `contract_blackbox/`、但實際透過原生 COM pair 執行的案例，應登記為 L1.5；不得同時重複計入 L1 與 L1.5 的案例覆蓋率。

Python pytest 不因數量接近 1.7k，就自動成為 Rust 實作的驗收證據。Rust 必須由自己的單元測試，以及真正指向 Rust artifact 的外部契約、L1.5、L2、L3 測試承接。

### 11.2 跨平台分類與執行資源

既有 pytest 保留以下分類，但必須另加「所需資源」標記，避免將記憶體替身誤認為原生平台覆蓋。

| 分類 | 必跑環境與內容 | 限制 |
|---|---|---|
| `core_portable` | Linux、Windows；純邏輯、記憶體 FakeSerialPair、契約序列化、可控時鐘與容量限制 | 此集合禁止以平台原因 SKIP；不得宣稱測到原生 UART |
| `posix_only` | Linux；PTY、POSIX 檔案／裝置鎖、適用的 `/proc` 行為與 `/dev/ttyMCU` | 真正 systemd、裝置交接等需要相應 runner，不能只靠 unit 檔案內容檢查 |
| `windows_native` | Windows；檔案分享旗標、鎖、行程與排程器、原生裝置介面 | 再區分免驅動、需 COM 裝置、需登入桌面／管理權限，不得整批籠統標為已覆蓋 |

資源標記至少區分：

`memory_only`、`process_rpc`、`posix_pty`、`windows_com`、`physical_uart`、`supervisor_runtime`、`interactive_desktop`。

**`windows_com` 必須由原生 Windows serial backend 真正開啟 COM 裝置。**Windows 通訊資源的開啟與獨占語意屬於正常的 `CreateFile` 通訊裝置路徑，不能用 socket 或記憶體 stream 替代後仍宣稱已通過。:chatgpt-content-reference{index="4"}

Rust 業務 crate 禁止平台條件編譯分支；但 `#[cfg(test)]` 與經核准、非平台差異的測試設定不在禁止範圍內。平台相依的 production code 仍限於 `sw-pal`。

### 11.3 契約、oracle 與案例承接清單

建立機器可讀的案例承接清單，至少記錄：

`case_id`、契約／不變量、所屬層級、平台、所需能力、承接切片、前置功能、必要性、既有 issue／行為變更編號、預期結果及證據檔案。

35%／60%／85%／100% 承接率只作進度資訊，不取代切片出口條件。每個切片的必要案例集合，必須在開始實作前凍結；不得在測試失敗後刪除案例、縮小分母或改標為非必要。

切片尚未實作的功能須列入未承接清單，不能以執行時 SKIP 取得「全綠」。若跨切片依賴使原定案例无法執行，必須先修訂切片範圍或出口歸屬，經審閱後再執行。

黑箱測試的受測入口為實際部署 artifact。測試端不得 import `sw_core`，亦不得連結 `sw-core`、`sw-pal` 或使用產品內部邏輯計算預期結果。允許讀取語言無關的契約 schema、案例資料及經審閱的測試專用 helper。

所有切片均須依其已承接功能驗證以下不變量：

- 被拒絕且依契約應為零副作用的請求，外部 TX 必須為零。
- 不得將不同來源的 bytes 以契約禁止的方式交錯。
- Recovery／Release／Reboot／Lease 切換不得回放失效上下文的輸入。
- 舊 I/O worker 未完成交接前，不得另開同一受管端點，亦不得回覆釋放成功。
- 已有或可能有裝置副作用的命令不得自動重送。
- 證據不足、擷取失敗或測試未完成不得轉為 PASS。

### 11.4 測試結果、例外與稽核

保留既有套件原始結果；新增的整合報告以外層 envelope 記錄 gate 判定，不要求為此重寫 TestPilot 或 `realhw` 引擎。

| 結果／標記 | 使用條件 | 對必要閘門的影響 |
|---|---|---|
| `PASS` | 所有必要 assertion 與證據完整性檢查通過 | 可計入通過 |
| `FAIL` | 產品行為違反契約或已核准 SLO | 阻擋 |
| `SKIP` | 已事先登記的能力不適用，例如該板無指定 bootloader | 不等於 PASS；必要能力缺失仍阻擋相應出口 |
| `XFAIL` | 明確 issue、固定失敗特徵與核准期限的既有缺陷哨兵 | 保留可見例外，不得納入「零缺陷」宣稱 |
| `XPASS` | 既有哨兵未再重現 | 需確認真修復與行為變更，不能自動刪除哨兵 |
| 基礎設施錯誤／證據不足 | bridge／DUT／runner 故障、擷取缺口、案例未完整執行 | 記錄原因並阻擋；不得誤報產品 PASS |

既有 `f4-background-result-tail-consistent` 哨兵依第 15 章保留。案例執行者不得自行修改 oracle 遷就受測版本。

報告須可追溯至實際受測組合，至少保留 CLI 路徑與 artifact hash、daemon 版本與 artifact 識別、契約 revision、案例清單 hash、測試工具 revision、平台與裝置資料，以及所需證據檔案。工具新增的 `HARNESS_*` 原因碼屬於測試工具契約，不得混入 serialwrap 的產品錯誤碼目錄。

### 11.5 CI、bench 與發版執行策略

| 時機 | 必要執行集合 |
|---|---|
| **每個 PR** | 雙平台 L1、適用平台測試、契約 schema 與錯誤碼政策檢查、Linux L1.5 核心 smoke；修改測試工具時加跑工具自測 |
| **影響 Windows PAL／Serial／取消／關閉／裝置交接的 PR** | 加跑合格 Windows runner 上的原生 serial 案例；未取得證據不得只憑免驅動 CI 放行 |
| **排程整合測試** | 雙平台 L1.5 完整集合；依 bench 資源執行 L2 非破壞集合與 L3 P0／相關 P1 |
| **切片出口** | 本章 §11.6 指定集合，及所有先前已承接功能的必要回歸 |
| **Release candidate／正式退役** | L2 全部適用案例，含已授權破壞性測試；L3 完整穩定性集合、48 小時長跑及 Canary／回滾證據 |

不要求每個一般 PR 都跑 48 小時，也不假設 hosted `windows-latest` 已具備 com0com、互動桌面或實體設備。依 OD-08，Windows 原生 serial 驗證由擁有者本機 Windows 主機作為自架 runner 提供：Spike 1 通過則以 com0com DUT-side pair 接 bridge，不通過則以實體 USB-UART 直接接 Windows 側（不經 usbipd）。動到 PAL 的 PR 與各階段出口必須取得此證據。

L2／L3 必須使用既有 bench 互斥協定；在同一 live bench 上，維持禁止並行 pytest 的既有約束。其他完全隔離、沒有共用 UART 的 CI runner 不受此禁令影響。

Self-hosted bench 不得直接執行未經核准的不受信任 PR 程式碼。破壞性測試必須顯式授權，並限制目標裝置與測試範圍。

### 11.6 各階段出口條件

下表為 v6 的階段出口要求，核准範圍依第 14 章，執行條件以各項適用能力及有效證據判定。OD-09 明列延後凍結的項目依其指定期限完成，其餘已裁決事項不得再標為待裁決；核准、實作與驗收狀態分別記錄，不得默認任何出口已通過。

| 階段 | L1／L1.5 出口 | L2 出口 | L3／平台出口 |
|---|---|---|---|
| **Stage 0：Python 基線** | BC-01～05 契約與安全案例通過；bridge 通過獨立自測；既有 13 個 func-test case 保留，建立適用的外部模式；Windows 原生 serial 路徑取得證據 | 全部適用非破壞案例通過，僅保留已核准哨兵；F12 WAL oracle 完成核准修訂；BC-02／03 有對應案例 | P0＋P1 適用集合通過；依 OD-10，Linux、Windows 僅各執行一次修正後 Python 連續 48 小時基線，修正前 32 小時資料僅作歷史參考。基線依執行前已凍結的絕對條件驗收，並提供 OD-09 延後項目的校準資料；不得以基線與自身比較宣稱不退化，也不得將事後凍結的門檻回填為事前已通過。正式 Stage 0 出口須完成延後項目的凍結與證據充分性核對；必要的 >72 小時監管驗證另案完成。 |
| **Stage 1A：Rust CLI 資料面** | 已承接命令全部通過 Rust CLI→Python daemon 黑箱與 L1.5；未承接管理命令的透明委派有測試證據 | 以 Rust CLI 執行資料面相關案例 | 以 Rust CLI 執行 P0 與相關 P1；此階段不單獨宣告完整生產檢查點 |
| **Stage 1B／Stage 1 完成** | 全部 20 群組與終端操作承接；版本閘門、安裝、監管、remote、回滾與效能指標通過 | 以 Rust CLI 作為 pinned `serialwrap_exe`，全部適用案例通過，含已授權破壞集 | P0＋P1＋remote 通過；Rust CLI＋Python daemon 完成 48 小時混合組合長跑，至少涵蓋 Windows 生產目標環境 |
| **Slice 2.1** | PAL／WAL／Arbiter／Shell 核心及最小安全閘門通過；含零 TX、部分送出中止、Drain／Release、最小 reboot 防護與獨占 | F1、F2、F3、F4、F12 的切片承接清單通過 | P0 適用核心集合＋WAL／裝置衝突／Recovery 相關 P1；雙平台均有原生 serial 證據 |
| **Slice 2.2** | Human／Agent 共存、Raw／Line、Telnet、Lease、Suspend／Resume、DeferredBuffer 與 reboot 交互案例通過 | F5、F6、F8 的切片清單，及先前切片必要回歸通過 | P0＋Console／Lease 相關 P1；Windows-native Console 案例不得僅以 Linux 結果替代 |
| **Slice 2.3** | 完整 Login FSM、Quiet Gate、Event Engine、Stall／Recovery；延遲與背景雜訊案例通過 | F9、F10、F11 的切片清單，及先前必要回歸通過 | P0＋登入／Recovery／事件相關 P1；適用 remote 案例通過 |
| **Slice 2.4** | 所有凍結契約承接；File Transfer、完整裝置交接、適用平台 MCU flash 與監管整合通過 | L2 的全部適用 37 個既有案例及新增核准案例通過，包含 F7；P1 裝置交接依既有 `realhw` 引擎列入本切片 L3 出口。適用的 P2-1 MCU flash 手動程序亦為本切片必要出口，Stage 0 須明列其程序 revision、執行入口、層級、責任人及證據，不得將其直接混入既有 L2 37 case 或 L3 36 case 計數。 | P0＋P1＋remote 通過；雙平台 Rust daemon 各完成 48 小時 `lr-mixed`，滿足絕對與相對閘門 |
| **Stage 3：Canary／Python 退役** | Release artifact 的完整契約與 L1.5 回歸通過 | 發版前全部適用案例通過，含已授權破壞集 | 相同 release artifact 的 48 小時 Sequential A/B 證據有效；Linux、Windows 各達 Canary 門檻並通過實機回滾 |

F1～F12 的切片對應沿用第 15 章草稿，並非已重新逐 case 核對的結果。Stage 0 須把它展開成精確 case ID 與相依性清單。若一個 family 含後續功能，必須預先明列跨切片案例，不能執行到一半才用 SKIP 通過出口。

Stage 0 修正可先以個別安全／功能 gate 合併，不必讓所有開發活動等待長跑；但沒有完成上述證據前，不得宣告「正式 Python 基線出口完成」。

Slice 2.4 的長跑可供 Stage 3 引用，但受測 binary、設定、契約、測試引擎、工作負載及關鍵環境須一致。更動相關 runtime 行為後必須重跑，不得引用舊 artifact 的 PASS。

依 OD-10，Canary 為 Linux、Windows **各 14 個活躍測試日、各至少 5,000 次 `cmd submit`、各至少 50 次適用的干擾／交接循環**，以**真實使用**累計（Windows＝測試團隊工作站、Linux＝擁有者日常使用），不佔用 bench。Windows 不要求執行 POSIX 專屬 `/dev/ttyMCU`，但不得以 Linux 的大量執行量掩蓋 Windows 未測。

### 11.7 效能與可靠度量測口徑

L1.5 的 Docker／Python bridge 路徑主要用於功能與故障語意，不作為 UART 電氣延遲或產品效能微基準的替代。

WAL、CLI、Console 延遲須分別量測，凍結 payload 分布、實際 bytes、每埠負載、取樣數、計時區間、快取條件與觀測工具開销。性能數據必須標示受測平台與 artifact，不得將模擬器耗時算成 serialwrap 退化，也不得將它從量測中任意扣除。

48 小時 PASS 只代表指定環境及負載下完成該次驗證，不代表已證明所有環境的可靠度。長跑比較方法、分母與失敗判準依第 15 章執行。

## 12. Windows 安裝／更新／回滾架構與四階段遷移計畫

### 12.1 Windows 發佈、更新與回滾

使用者模式的版本資產置於：

`%LOCALAPPDATA%\Programs\serialwrap\versions\<version>\`

穩定入口置於：

`%LOCALAPPDATA%\Programs\serialwrap\bin\`

設定與狀態依 PAL 的使用者路徑；`windows-service` 採核准的系統路徑與 ACL。穩定入口的實作方式須在 Windows 實測，不預設可依賴需要額外權限的 symlink。

每個版本目錄包含 release manifest。Stage 1 的版本單位是 Rust CLI、Python daemon runtime／程式碼、設定資產及監管定義的配對組合，不是單一 CLI 檔案。

更新流程：

1. 驗證新 artifact 與 manifest，先放入未啟用版本目錄。
2. 檢查 Flash、FileTransfer、活躍寫入與其他不可安全中止操作；不符合交接條件時拒絕更新。
3. 停止該實例的監管拉起能力，再要求 daemon 安全停止。
4. 確認 I/O worker、UART handle 與實例資源已釋放。
5. 備份必要設定與狀態，切換穩定入口及監管目標。
6. 啟動新版本，驗證版本配對、health、狀態相容及可操作性。
7. 失敗時回到完整 Previous 組合，保存失敗證據。

`setup --rollback` 不得只切換 CLI 而保留不相容 daemon。Drain 未完成時不能強行啟動上一版搶同一 UART。

遷移期間不寫入舊版無法讀取的 `state.json`；如确需不相容變更，必須先核准遷移／回復方案，不能在實作後才宣布無法回滾。

至少保留 Current、Previous 及對應狀態／設定備份。回滾驗收須包括真實串口釋放與重新取得，不只確認版本字串改變。

### 12.2 四階段遷移、出口與校準

第 11 章是測試集合與 gate 的權威，本節是階段範圍與推進條件的權威。第 15 章定義實際引擎、工作負載與證據。相同規則以引用維持一致，不能各章自行降低要求。

v4 的 Stage 0「2～3 週」、Stage 1「3～4 週」、Stage 2「14～20 週」僅保留為歷史假設。依 OD-13，下表依工作包重新估算（2026-10-15）。**這是規劃估計，不是交付承諾**；Stage 0 出口與 Slice 2.1 完成時各重新校準一次。

首次排程前，須補列工作包與資源核算：Spike 5 的 Stage 0 BC-04／交接驗證、Spike 8 長跑故障保留與觀測、Windows L2／L3 adapter 與案例移植、>72 小時監管資格，以及 Stage 2 雙軌維護與回歸重跑。各項須指定負責人、承接階段、人工作業量、連續運行時間、bench 佔用及可否並行；既有 48 小時長跑天數不得視為已包含這些工作。

| 階段 | 主要工作包 | 估計（日曆週） | 估計依據與 bench 佔用 |
|---|---|---|---|
| Stage 0 | BC-01（含 Windows `CreateFileW` 常駐開檔、自癒、缺口紀錄）與 F12 oracle 修訂；BC-02～05；錯誤碼目錄與 paulsha-conventions 通用規則（OD-06）及其發版／pin 更新；契約 schema、黑箱契約測試與 hosted Windows CI；`uart-stdio-bridge`（socat 驗證→自寫工具、byte 紀錄 schema、情境腳本、Docker DUT 模擬器、自測）；擁有者 Windows 主機自架 runner 與 Spike 1／2／3／4／7；Linux／Windows 修正後 Python 48 小時基線各一次 | **6～9 週** | 工作包可部分並行；48 小時基線兩平台依序執行，bench 約佔 5 天（OD-10） |
| Stage 1 | 1A 資料面命令；1B 管理面、`windows-task` 安裝／監管、更新與回滾、版本配對（Spike 6）；混合組合 Windows 48 小時長跑 | **4～6 週** | bench 約佔 3 天 |
| Stage 2 | Slice 2.1～2.4，含前移至 2.1 的最小安全閘門與 Windows adapter | **16～24 週（假設）** | 以 v4 的 14～20 週為底，加計安全前移與 Windows adapter；Slice 2.1 後依實測速率重算，超過 24 週須重提 OD-02。2.4 出口兩平台 48 小時長跑約佔 bench 5 天 |
| Stage 3 | 核對正式 release artifact 的有效 Sequential A/B 證據，補足失效或缺漏項目，完成回滾演練與真實使用 Canary | **暫估 3～4 週，實際以全部 Canary 配額與事故處置完成為準** | 符合 §11.6 證據沿用條件者不重複長跑；兩平台 Python／Rust 四輪均須重跑時，另約佔 bench 9 天。Canary 可重疊進行，但不得以日曆期限取代執行配額 |
| **合計** | | **Stage 0～2 約 26～39 週，另加 Stage 3 實際達標時間；Stage 3 若為 3～4 週，合計約 29～43 週** | 依本表估計，Stage 0～2 長跑含切換約佔 bench 13 天；Stage 3 另依有效證據缺口增加 0～9 天。OD-10 的約兩週為規劃估計，不代表所有重跑均已包含；監管資格、其他實機測試與故障重跑另列資源需求，由擁有者排程 |

#### Stage 0：Python 基線與測試基礎

完成 OD-04 已核准 BC、兩個診斷命名空間、契約／案例清單、F12 oracle 修訂、bridge 自測與必要外部情境、Windows 原生 serial 資格及既有回歸／長跑整合。

出口須取得 §11.6 的 L1、L1.5、L2、L3 證據，包含 Linux／Windows 分別的基線及適用監管測試。必要決策未完成、能力缺失或只有 SKIP，不得宣告正式 Python 基線完成。

個別修正可在其必要測試完成後合併；不要求所有開發活動等待 48 小時，但正式階段出口不能省略長跑。

#### Stage 1A：Rust CLI 資料面

依 §4.2 承接資料面，未承接命令依 §4.3 安全委派。使用 Rust CLI→Python daemon 執行相同黑箱與 L1.5、資料面 L2、P0／相關 P1。

1A 不單獨構成完整生產檢查點。

#### Stage 1B：Rust CLI 完整發佈組合

完成管理／終端能力、安裝、監管、remote、版本配對與實機回滾。

出口包含全部適用 L2、P0＋P1＋remote、性能指標，以及 §11.6 指定的混合組合 48 小時長跑，至少涵蓋 Windows 主要使用環境。

通過後才可作為 Stage 2 的正式退守版本。

#### Stage 2：四個垂直切片

| 切片 | 必備範圍 | 主要出口 |
|---|---|---|
| **2.1** | PAL、WAL、Arbiter、Passthrough／Shell；**最小 Quiet、帳密未解析終態、送出前 Gate、部分／不明 TX 處置、Drain、Release／Attach、獨占與有界資源** | §11.6 的 F1／F2／F3／F4／F12 承接清單、核心 P0／P1、雙平台原生 serial 與安全 L1.5 |
| **2.2** | Human Console、PTY／Telnet、適用的 Serial Console、Raw／Line、Lease、Soft Preempt、Suspend／Resume、DeferredBuffer | F5／F6／F8、Console／Lease P1、Windows-native Console 與跨 reboot 情境 |
| **2.3** | 完整 Login profile、Quiet／Reconfirm、Event Engine、LineBuffer、Stall／Recovery | F9／F10／F11、相關 P1／remote、延遲與背景雜訊情境 |
| **2.4** | FileTransfer、完整外部裝置交接、適用的 MCU flash／holder-probe、監管整合 | 全部凍結契約、適用 L2、P0＋P1＋remote、雙平台 Rust daemon 48 小時長跑 |

**2.1 已會送 bytes，因此安全骨架不能等 2.3 或 2.4。**尚未完整支援的 profile／能力必須在開始切片前列出，不能把 Gate 關掉後先接真實板子跑一般工作。

每個切片除新增集合外，還須通過先前承接功能的必要回歸。family 內跨切片案例由 Stage 0 展開成 case ID；不能臨時以 SKIP 解決範圍矛盾。

35%／60%／85%／100% 只保留為進度參考，不作出口替代。

#### 契約先行的雙軌開發

尚未開工切片的新 behavior，先建立黑箱契約，再在 Python 交付；進入切片時承接。

已開工／完成切片的新 behavior，先建立契約，再同步維護相應 Python／Rust 基線，或由擁有者明確裁決交付範圍。不得讓新行為只存在於其中一側卻宣稱差分基準一致。

雙軌同步超過當雙週重構投入的 30% 時，暫停開立下一切片並重新排程。測試工具同步、實機重跑與缺陷處理均計入實際投入，不隱藏在「非 coding 工時」。

#### 校準與斷路器

Slice 2.1 完成後，以實際契約包、安全里程碑、平台缺陷、回歸重跑與 bench 等待時間重新估算，不以 Python LOC 直接除以固定每週行數。

Stage 2 自 Slice 2.1 開始至 Slice 2.4 出口的預估總日曆時間，包含已耗時間與剩餘預估，若超過 24 週，須重新提交 OD-02 裁決；bench 等待、雙軌同步與必要重跑均計入，不以僅剩餘時間判斷，也不將 Stage 3 混入此門檻。

依 OD-02／OD-13，連續三週內，當前切片凍結的必要案例承接率累計增加不足 10 個百分點時，即觸發斷路器，停止候選版本擴大部署並退守已驗收的 Stage 1 發佈組合，再提請擁有者審查後續投入；安全／平台里程碑列為審查證據，不自動豁免斷路器。承接率以取得有效通過證據的必要案例數除以凍結的必要案例總數計算，觀測窗口起訖於切片開始時固定。不得透過拆案例或改分母規避。分母與起訖時點於每個切片開始時凍結。

發現危險 TX、交接失控或無法回滾時，不必等待三週，立即停止候選版本擴大部署。

#### Stage 3：Canary 與 Python daemon 退役

使用正式 release artifact 完成 §11.6 的回歸、有效 48 小時 A/B、Canary 與回滾。

**依 OD-10，Linux／Windows 各 14 個活躍測試日、各 5,000 次提交、各 50 次適用干擾／交接循環，以真實使用累計，不佔 bench。**48 小時 A/B 長跑則在擁有者 bench 上 Linux、Windows 依序執行。循環類型配额须预先定义，不能全部以同一種容易的 Recovery 取代。

**依 OD-09，1% 非預期錯誤率、95% 基線吞吐、卡態不退化已凍結；RSS／資源預算、最低有效負載與比較容許誤差於 Stage 0 修正後 Python 48 小時基線完成後、任何 Rust 候選長跑前凍結。**未凍結的項目不得宣告依該門檻完成發版驗收。

非預期重啟即使被 supervisor 自動修復，仍須列為事故；未結案事故、安全不變量違反或必要證據缺漏均阻擋退役。Canary 開始前，由擁有者凍結活躍日認定、提交計數與去重方式、干擾／交接分類及配額，並指定各平台證據責任人。每筆累計須關聯日期、平台、artifact 與結果；修復造成 artifact 變更時，須先判定既有配額與證據的有效範圍，不得直接合併不同版本的執行量宣告新版本通過。Stage 2.4 證據僅能在 artifact、契約、工具、工作負載與關鍵環境一致時供 Stage 3 引用。

退役範圍為產品 Python daemon，不包括 Python 測試、bridge、DUT 模擬器及既有 TestPilot 引擎。

## 13. Mandatory Spikes 與能力資格

Spike 結果只對指定環境與操作有效。每個結果須保存環境、artifact、操作、觀測與退路；不得只留下「可用／不可用」結論。

| 編號 | 目的與階段 | 必要驗證 | 失敗時處置／相依裁決 |
|---|---|---|---|
| Spike 1 | Stage 0：Windows 虛擬 COM 資格 | 在 OD-05 機台驗證 driver、Secure Boot／HVCI、DUT-side 與 Console-side pair、原生開埠、控制線隔離；分別記錄能力 | Console 退路依 OD-01；原生 Serial 測試退路依 OD-08。不能關閉安全功能後仍宣稱符合原標準 |
| Spike 2 | Stage 0：Task／Service | 本輪必驗 `windows-task` 的登入觸發、唯一實例、失敗重啟、`PT0S` 與實際 >72 小時運行，並驗證採用的 on-demand 退路。`windows-service` 的帳號、Session 0、路徑及授權驗證，僅在符合 OD-12 且核准採用時列為必要資格。Stage 1B 須核對 Stage 0 證據與正式 artifact、監管定義及啟動入口是否一致；有影響的變更須重驗。 | 依 OD-03／OD-12 調整部署；48 小時長跑不能取代此項 |
| Spike 3 | Stage 0：Windows WAL | 實際端點防毒（代表機台為 第三方企業端點防毒（EDR）；Windows Defender 關閉）開啟下的一般寫入、正常 rotate、unlink、外部 reader 持有 handle、重建 timeout、F12 實際進展 | 凍結 BC-01 與性能口徑；不強制關外部 handle、不恢復每筆開關檔來遷就舊 oracle |
| Spike 4 | Stage 0：bridge 證據資格 | 完整 byte 值域、短讀／部分寫入、stderr flood、EOF、DUT restart、transport fault、證據損壞與空間不足 | 未通過自測前只作接線工具，不作 gate 證據；位置依 OD-07 |
| Spike 5 | Stage 0～2.1：Serial 獨占與 Drain | 外部先占用、競爭開埠、取消完成／取消失敗、晚到 completion、timeout 不阻塞 Actor、Release 回條 | 能力缺失阻擋相關切片；不以新 handle 繞過卡住的舊 handle |
| Spike 6 | Stage 0～1：混合版本與回滾 | 固定 Rust CLI＋Python daemon 組合、委派無遞迴、版本拒跑、artifact 可追溯、實機釋放與回滾 | 配對策略依 OD-13；禁止只改版本字串通過 |
| Spike 7 | Stage 0 起：Windows 測試 adapter | Telnet Human frontend、程序／RSS 觀測、共用 bench lock、原生 COM case、既有 regression／realhw 引擎沿用 | 缺必要能力列阻擋；不以大量 SKIP 宣稱 Windows 完成 |
| Spike 8 | Stage 0 起：長跑與故障保留 | 保留 per-board lock 的原 `lr-mixed`；隔離自動重啟；確認 worker 持續負載、未結案命令與卡態觀測精度 | SLO 依 OD-09，平台資源依 OD-10；不得拼接短 segments 成 48 小時 |
| Spike 9 | 多使用者部署前：IPC 信任邊界 | 另一個未授權本機使用者對 RPC／Console 的實際存取；設定 ACL 與連線授權分開驗證 | OD-12 未核准或隔離未完成前，限制部署範圍 |

Spikes 不建立新的測試引擎。可用一次性資格程序完成的結果，應收斂為既有套件的 capability、案例或 preflight；持續需要回歸的行為須進入第 11、15 章的清單。

## 14. Owner Decision Gate

### 14.1 裁決結果摘要（2026-10-15）

OD-01～OD-13 已全部裁決。裁決改變的是「核准範圍」，不代表已實作或已驗收；相依於延後凍結項目（OD-09 第二部分）或尚待實測（Spike 1～9）的出口，在條件成立前維持阻擋。

| ID | 議題 | 裁決 |
|---|---|---|
| OD-01 | Serial Console 產品定位 | A：TCP＋Telnet 必備；com0com Serial 選配，驅動無法部署不阻擋 Windows 發佈 |
| OD-02 | Stage 2 啟動承諾 | A：Stage 0／1 出口通過後直接啟動 Slice 2.1（附條件，保留斷路器） |
| OD-03 | Windows 預設監管 | A：工作站預設 `windows-task`；Service 僅限 OD-12 條件成立的無人值守 bench；on-demand 為退路 |
| OD-04 | BC-01～BC-05 | 全數核准為 `APPROVED` |
| OD-05 | 代表機台與安全政策 | 擁有者本機 Windows 11 Pro 24H2、HVCI 開啟、Secure Boot 關閉、可自裝、第三方企業端點防毒（EDR） |
| OD-06 | 錯誤碼說明規則位置 | A：paulsha-conventions 通用規則，不寫死錯誤碼 |
| OD-07 | bridge 位置 | A：serialwrap repo 獨立目錄 |
| OD-08 | Windows 原生 serial 驗證 | A＋C：擁有者本機自架 runner；com0com DUT-side pair，不行則實體 USB-UART |
| OD-09 | 長跑發版門檻 | 三項立即凍結；資源預算、最低負載、容許誤差於 Python 基線後、Rust 長跑前凍結 |
| OD-10 | 長跑與 Canary 資源 | 精簡版：bench 依序長跑、Stage 0 只跑修正後基線、Canary 以真實使用累計 |
| OD-11 | 隔離測試裝置 allowlist | 核准（附限制） |
| OD-12 | Windows IPC 信任模型 | A：整機本機使用者可信，僅部署單人工作站 |
| OD-13 | v5 配套範圍與時程 | 一併核准，依 §12.2 重算工期 |

### 14.2 長跑發版門檻（OD-09）

| 項目 | 規則 | 狀態 |
|---|---|---|
| 非預期命令錯誤率 | 不超過 1%，且不高於對應 Python 基線 | **已凍結（2026-10-15）** |
| 有效完成吞吐 | 每板、每來源不低於對應基線的 95% | **已凍結（2026-10-15）** |
| `stuck_attached` | 相同負載下的總時長與最長連續區段不高於基線 | **已凍結（2026-10-15）** |
| RSS／資源 | 峰值、穩態預算與趨勢限制，依指定硬體及負載 | **延後凍結**：Stage 0 修正後 Python 48 小時基線完成後、任何 Rust 候選長跑前 |
| 最低有效負載與樣本 | 各板、來源、命令模式均須達預先指定的有效工作量；未結案命令不得消失 | **延後凍結**：同上 |
| 比較不確定性 | 事前指定容許差異、複驗條件及證據不足判定；不能挑選最好一輪 | **延後凍結**：同上 |

這些值不是由既有 32 小時、31 segments、49,899 submitted 的資料推算出的已驗證 SLO，也不是既有 `lr-mixed` 已實作的引擎門檻。

既有引擎的重大事件、daemon death、單板非 READY ≥15 分鐘及錯誤率 >50% 等 FAIL 判準保留原始結果。新增發版判定以外層 `release_gate_result` 表達，不改寫原始 `engine_result`。

計數須區分 submitted、accepted、completed、failed、unresolved，以及預期拒絕與非預期錯誤。具體分母在執行前凍結，不能把被拒絕、卡住或停止提交的工作任意移出統計以改善結果。

安全不變量（未授權 TX、雙重持有、跨上下文回放、危險自動重送）必須為零，不能以「Python 也會發生」豁免。門檻不得在看到候選版本結果後修改。

### 14.3 裁決紀錄

| ID | 裁決 | 日期 | 備註 |
|---|---|---|---|
| OD-01 | **A**：TCP＋Telnet 為必備基準；com0com Serial（COMx）為選配，僅在驅動可部署的機台啟用 | 2026-10-15 | Spike 1 失敗時不另行採購驅動；OD-08 的 Console-side 測試依此為選配範圍，DUT-side 原生 Windows serial gate 不受影響 |
| OD-02 | **A**：Stage 0／1 出口通過後直接啟動 Slice 2.1，以實測速率校準後續切片 | 2026-10-15 | 附條件承諾：前置 OD-04、OD-10、OD-13 與 Stage 0／1 gate；斷路器（連續 3 週契約測試無實質進展）觸發即退守 Stage 1 |
| OD-03 | **A**：測試團隊工作站預設 `windows-task`（登入自啟、失敗自動重啟、使用者身分、關閉 72 小時執行上限） | 2026-10-15 | `windows-service` 僅限無人值守 bench 且 OD-12 條件成立時採用；`on-demand` 保留為退路；各模式須完成 Spike 2 |
| OD-04 | **BC-01～BC-05 全數核准**（PROPOSED → APPROVED），連同 v5 §9.1 所列核准範圍與必要驗證 | 2026-10-15 | BC-01 的 F12 oracle 修訂仍由 OD-13 配套；BC-04 的平台證據依 OD-05／OD-08；BC-04 使 daemon 持有裝置時無法再直接開 tty（應走 serialwrap console），屬預期行為 |
| OD-05 | **代表機台＝擁有者本機 Windows 主機**：Windows 11 Pro 24H2（registry ProductName 顯示 Windows 10 為已知顯示問題）；HVCI 開啟且執行中、VBS 執行中；Secure Boot **關閉**；使用者可自行以管理員權限安裝；端點防毒為 **第三方企業端點防毒（EDR）（執行中）**，Windows Defender 關閉 | 2026-10-15 | 2026-10-15 自 WSL 唯讀查詢取得。**更正**：v5 中「Defender」相關敘述（含 16.8 ms WAL 歸因、Spike 3）改為「實際端點防毒（本機為 第三方企業端點防毒（EDR））」。代表性風險：若測試團隊機台開啟 Secure Boot，驅動載入條件更嚴，須另行記錄 |
| OD-06 | **A**：規則直接進 paulsha-conventions，為**通用規則、不寫死錯誤碼**：「凡程式會輸出的 error code，都必須在 help／SKILL 有對應說明（意義與處置）」 | 2026-10-15 | 規則本身不得內建任何錯誤碼清單；各 repo 以設定宣告「如何抽出會輸出的 code」（含常數傳遞路徑）與「說明文件位置」。v5 §9.2 的 78 個清單僅為現況盤點，不得成為規則或政策的硬編碼依據；`error_code` 與 session `last_error` 兩個命名空間都適用 |
| OD-07 | **A**：`uart-stdio-bridge` 放在 serialwrap repo 的獨立目錄 | 2026-10-15 | 比照 `realhw/`、`regression/` 模式：獨立依賴、自測與 byte 紀錄 schema；禁止 import 產品程式碼、不得共用產品判定邏輯；驗收時固定工具 revision |
| OD-08 | **A＋C**：擁有者本機 Windows 主機作為自架 runner；Spike 1 通過則以 com0com DUT-side pair 接 bridge，不通過則以實體 USB-UART 直接接 Windows 側（不經 usbipd） | 2026-10-15 | 一般 hosted PR 不裝驅動；動到 PAL 的 PR 與各階段出口必須取得原生 Windows COM handle 證據；記憶體 fake 不得充數。Console-side COM 依 OD-01＝選配 |
| OD-09 | **立即凍結三項**：非預期命令錯誤率 ≤1% 且不高於對應 Python 基線；每板每來源有效完成吞吐 ≥ 基線 95%；`stuck_attached` 總時長與最長連續區段不高於基線。**延後凍結**：RSS／資源預算、最低有效負載與樣本、比較容許誤差——於 Stage 0 修正後 Python 48h 基線完成後、任何 Rust 候選版本長跑開始前凍結 | 2026-10-15 | 安全事件為零與「不得看完結果再改門檻」為不可調原則；既有引擎 FAIL 判準保留為 `engine_result`，發版判定以 `release_gate_result` 表達 |
| OD-10 | **精簡版**：(1) 48h 長跑一律在擁有者 bench 上 Linux／Windows **依序**執行（同機同板經 usbipd，不可並行），由擁有者排程；(2) Stage 0 只跑**修正後** Python 48h 基線（兩平台各一次），修正前以既有 32h 歷史資料參考、BC-01 效能以微基準與短測驗證；(3) Canary 配額照舊（每平台 14 活躍日、≥5,000 次 `cmd submit`、≥50 次干擾／交接循環），但以**真實使用**累計：Windows＝測試團隊工作站、Linux＝擁有者日常使用，不佔 bench | 2026-10-15 | 預估 bench 佔用由約 48 天降至約 2 週；長跑期間 bench 互斥鎖與其他 TestPilot 套件共用，排程需避開；canary 期間非預期重啟仍計事故 |
| OD-11 | **核准（附限制）**：Windows 測試專用虛擬裝置白名單，作為 Linux `SERIALWRAP_BY_ID_DIR` sandbox 的對應機制 | 2026-10-15 | 限制：僅對隔離實例（獨立 state／run 目錄）生效，正式 daemon 設定遇到即拒絕；只接受明確列出的 COM 名稱，不得全域放行 com0com；不關閉獨占鎖、不更換 serial backend；啟用時 `daemon status` 必須顯示 |
| OD-12 | **A**：Windows RPC 與 Telnet console 的信任範圍明定為「整機本機使用者皆可信」，僅部署於單人使用的工作站（`windows-task`） | 2026-10-15 | 多人共用機器（含 `windows-service` bench）在完成多使用者認證／授權（RPC token＋ACL，且 Telnet console 一併納管）前不得部署；規格不得再宣稱已具「同使用者隔離」；`serialwrap remote` 經 ssh 暴露 RPC 時，信任範圍延伸至通過該 ssh 驗證者，須寫入規格 |
| OD-13 | **一併核准並重算工期**：F12 oracle 由 mtime 改為實際 WAL 進展（seq 前進＋檔案大小或讀回最後一筆）；Stage 1 Rust CLI＋Python daemon 版本配對記錄與檢查；效能量測口徑（Workload-S1、取樣、跨 64 MB rotate 停頓上限）；最小 Quiet Gate／Drain／Release／禁止危險重送前移至 Slice 2.1；斷路器「連續 3 週承接率 <10%」須定義分母與起訖 | 2026-10-15 | 不得沿用 v4 工期；Stage 0 起的工期依 OD-07（bridge）、OD-08（自架 Windows runner）、OD-10（bench 依序長跑約 2 週）重新估算 |

若後續裁決改變既有核准範圍，須新增紀錄（不覆寫舊紀錄），更新受影響契約與案例，並重新判定證據有效性；不得保留舊 PASS 標記而不標示其前提已失效。

## 15. 實機回歸、長時間壓力與外部 bridge 驗證

### 15.1 既有資產與整合原則

本章沿用既有測試資產，不另建平行回歸框架。

| 既有資產 | 本次沿用方式 |
|---|---|
| `func-test/` | 保留 YAML 驅動與既有 13 個 case；新增外部 DUT／bridge 執行模式 |
| `FakeTarget` | 保留既有 PTY 實作供快速測試；逐步分離測試專用的 DUT 行為與 POSIX I/O |
| `DaemonHarness` | 沿用暫存 profile／state／run 目錄與 by-id sandbox 概念，擴充部署 artifact 與外部端點接入 |
| `tools/docker/uart_harness.py` | 保留既有容器內 fake target＋daemon 與 remote 三拓樸驗收；不因新增 bridge 而移除 |
| `regression/` | 保留 TestPilot plugin `serialwrap_regression` 的 12 family、37 case |
| `realhw/` | 保留 P0×8、P1×20、remote×7、longrun×1，共 36 個既有 case |
| `serialwrap_reliability` | 保持為同一 `realhw` 引擎的 TestPilot 第二前端，不複製測試邏輯 |

L2／L3 既有「禁止 import `sw_core`，只經部署 CLI subprocess＋JSON 操作 live daemon」的邊界維持不變。

此邊界使案例可指向 Python 或 Rust artifact，但不保證所有測試基礎設施完全不用修改。版本識別、Windows Console／行程觀測 adapter、WAL 活性 oracle 等必要修訂，須明列並審閱；不得以「不改一行」為由保留錯誤 oracle。

### 15.2 L2／L3 依賴的契約與環境

第 9 章須凍結下列實際依賴。凍結內容不只是欄位名稱，也包含型別、單位、缺省／null 語意、終態定義、更新時機與分頁方式。

| 項目 | 必須凍結的內容 |
|---|---|
| **版本閘門，#154** | `daemon status.version` 與 pinned CLI `--version` 的比對規則；不一致時整場拒跑 |
| **WAL 診斷，#148／#101** | `wal_path` 的意義與可讀性、`doctor` 的 `wal_dir`；WAL 活性依新增紀錄／seq／指定 marker 驗證，不以 mtime 作唯一依據 |
| **多開與占用診斷** | `multi_open`、`foreign_holders`；`DEVICE_STILL_HELD`、`pids`、Windows `holder_unknown` 的確切語意 |
| **活動觀測，#153／#150** | `rx_bytes_last_10s`、`last_rx_age_s`、`last_tx_age_s`、`last_error_detail`；包含尚未收送資料時的表示方式 |
| **Self-test／profile** | `classification`，包含 `RX_FLOOD`、`BOOTLOADER`、`AUTOBOOT_QUIET`；`profile_bootloader_prompts` 檢查項 |
| **開機過渡，#139／#162** | `AUTOBOOT_QUIET`、`retry_after_s`、`READY_UNCONFIRMED`、`recommended_action`、`ready_reconfirm_pending`、`ready_reconfirm_failed` |
| **登入帳密，#140** | `CREDENTIALS_UNRESOLVED` 所在的實際欄位與終態語意；帳密未解析時不得送空帳密 |
| **命令與背壓，F1／F2** | `CMD_TOO_LONG`、`CMD_CONTAINS_NEWLINE`、`SESSION_QUEUE_FULL`、`FLUSHED_BY_RECOVERY`、`limits`，以及 BC-03 的 `COMMAND_ABORTED_IN_FLIGHT` |
| **Drain 診斷** | v4 已提議的 `drain_stalled`、`drain_timeout_epoch` 及其與 `FORCE_RECOVER_TIMEOUT` 的關係 |
| **WAL range，#224** | `truncated=true` 表示仍有下一頁，不表示資料遺失；下一頁取得方式須依既有實際契約凍結 |

`CREDENTIALS_UNRESOLVED` 已確定為 session `last_error` 值，依 §9.3 納入 `contracts/session_last_errors.yaml`；不得新增為 RPC／CLI `error_code`、SessionState 或 classification。Stage 0 僅擷取其尚未凍結的基本 SessionState、清除時機及空值表示，不重新判定命名空間。

Stage 1 的 Rust CLI＋Python daemon 必須是明確發佈的相容組合。兩者依 §9.5 使用同一 release bundle 版本，component implementation 與 artifact hash 另行識別；不得放寬既有版本拒跑閘門，亦不得只改版本字串通過檢查。

bench lock 與報告路徑屬於測試環境契約，不混入 RPC schema：

- Linux 保留共用 `~/.local/state/serialwrap/bench.lock`。
- 保留 `~/b-log/regression-reports/` 與 `~/b-log/realhw-reports/` 報告入口。
- Windows 須由 testbed 明確解析同一個共用 lock 絕對路徑，讓 regression、reliability 與其他 TestPilot 套件實際共鎖。
- 跨主機共用同一實體 bench 時，單機檔案鎖不足以協調，必須由 bench 排程／租用機制保證唯一持有者。

### 15.3 `uart-stdio-bridge` 的定位與拓樸

`uart-stdio-bridge` 是 **L1.5 的獨立 I/O 轉接與證據工具**，不是 broker、不是 RPC client，也不是新的 DUT 業務狀態機。

標準拓樸：

```text
測試協調器／Agent／Human Console
                │ CLI、RPC、Console
                ▼
       真正的 serialwrap daemon
                │ 原本的 serial backend
                ▼
     作業系統虛擬序列埠的受測端 A
                ↕
       PTY 或 Windows COM pair
                ↕
              對端 B
                │
       uart-stdio-bridge
          ├─ 獨立 byte 證據
          ├─ 傳輸故障控制
          └─ binary stdin/stdout
                ↕
       Docker 內 DUT 模擬器
```

serialwrap 與 bridge 應優先執行於待驗證的原生作業系統；Docker 只承載 DUT 模擬器。原生 Windows daemon 不得被移到 WSL／Linux 容器後，仍標示為 Windows backend 驗證。

方向一律以 serialwrap 為準：

```text
TX：serialwrap → 虛擬埠 → bridge → DUT stdin
RX：serialwrap ← 虛擬埠 ← bridge ← DUT stdout
```

bridge 只持有對端 B，不持有 serialwrap 開啟的端 A，也不與 daemon 競爭同一端點。

故障控制須經獨立控制通道或預載入情境執行。禁止把 reset、pause 等測試控制指令混入 DUT 的 UART byte stream，除非該案例本來就在測試這些裝置命令。

### 15.4 工具形態、語言與獨立性

#### 15.4.1 導入順序

**第一步：socat 接線 Spike。**

可用 `socat` 的 PTY＋EXEC 功能確認原始資料轉接、TTY raw 設定、EOF 與子程序連接。這只是拓樸可行性檢查，不作為正式差分證據格式。:chatgpt-content-reference{index="5"}

**第二步：Python 正式 bridge。**

建議首版使用 Python，POSIX 端採標準 PTY／termios 介面，Windows 對端使用固定版本的 pySerial。pySerial 提供 Windows 與 POSIX serial backend，适合將平台差異限制在測試工具的端點 adapter。:chatgpt-content-reference{index="6"}

選用 Python 的理由是沿用現有測試維護環境、降低新增工具成本，不是因為 Python 天生比 Rust 更獨立。bridge 不隨產品發佈，因此不影響產品端 Python daemon 退役。

只有在量測證明 bridge CPU／I/O 開銷妨礙指定測試負載，或部署需求確實要求獨立 binary 時，才評估另以 Go／Rust 實作。不得為了驗證 Rust 重構，再啟動一個沒有證據支持的工具重寫專案。

#### 15.4.2 與產品程式碼的邊界

bridge、DUT 模擬器及差分比較器不得 import／連結產品的 Serial、WAL、仲裁、登入 FSM 或 prompt parser 實作。

允許共用的是宣告式 schema、測試情境資料、標準 JSON／Base64／CRC 函式庫與測試專用 helper。不得共用的是「如何判定 serialwrap 正確」的產品內部演算法。

即使工具與產品使用不同語言，若共用同一套 parser／狀態機，仍可能產生共同失敗而互相判定正確。反之，工具放在同一 repo 並不妨礙建立獨立實作邊界。

依 OD-07，放在 serialwrap repo 的 `tools/uart_stdio_bridge/`，比照 `realhw/`、`regression/` 採獨立依賴、測試與審閱規則，並禁止 import 產品程式碼。驗收使用經核准的測試工具 revision，不能讓候選產品修改未經審閱的 oracle 後自我驗收。

#### 15.4.3 bridge 自身的必要行為

所有 I/O 使用 binary bytes；不得自動 decode、換行轉換、echo 或 strip ANSI。短讀與部分寫入須正確處理；每個方向有獨立讀寫工作，stderr 持續排空至診斷檔，不能阻塞資料流。

正常模式採有界緩衝。證據 writer 失效、空間不足或非預期 overflow 時，本次驗證失效並停止，不得靜默丟資料後繼續判 PASS。

正式作為 gate 前，bridge 須通過獨立自測：完整 byte 值域、多區塊資料、短讀／部分寫入、stderr 大量輸出、EOF、程序退出、故障控制、CRC／offset 驗證，以及故意破壞證據後比較器確實拒絕通過。

### 15.5 與 `FakeTarget`／`func-test` 的關係

採取 **並存、抽離測試專用行為、增加執行模式**，不直接取代既有 `FakeTarget`。

`FakeTarget` 保留為快速 POSIX PTY 假裝置。其登入步驟、指令覆寫、背景雜訊與暫停回應等測試專用行為，可逐步抽離成不依賴 PTY 的行為模型，再由兩種 adapter 使用：

```text
相同的測試情境資料／DUT 行為模型
        ├─ 既有 FakeTarget：POSIX PTY adapter
        └─ 新 DUT 模擬器：binary stdin/stdout adapter
```

這種共用只限測試 DUT 行為，不得把 serialwrap 本身的 Login FSM、prompt 比對或仲裁邏輯搬進來。

既有 13 個 YAML case 的 ID、目的與 oracle 必須保留。適用案例新增「外部 bridge 模式」；不能用新增一個較簡單案例，宣稱舊案例已被取代。不同模式的執行次數與獨立 case 數須分開統計。

既有 `DaemonHarness` 延伸為啟動指定 CLI／daemon artifact，以及接收外部端點資訊。它仍負責隔離 profile、state、run 與裝置 sandbox，但不代替 bridge 宣稱實際 bytes 已送達。

`tools/docker/uart_harness.py` 的原有 remote 三拓樸驗收維持；新增「DUT 在容器內、daemon 在主機原生 OS」是不同拓樸，報告中須分別辨識。

### 15.6 雙向 byte 證據與 WAL 對照

#### 15.6.1 證據產出

每次 L1.5 執行至少產出：

| 檔案 | 用途 |
|---|---|
| `manifest.json` | artifact、工具、情境、平台、端點與工作負載身分 |
| `wire.ndjson` | bridge 實際觀測／完成的 byte 事件 |
| `control.ndjson` | 故障控制、barrier、DUT 重啟及完成回條 |
| `dut.stderr.log` | 模擬器與 Docker 診斷，不進 UART |
| `correlation.ndjson` | 測試步驟、CLI request／cmd ID、外部 byte 區間與 WAL 的對應 |
| `comparison.json` | 不變量與差分判定、第一個差異位置、證據不足原因 |

原始 WAL 與 CLI stdout／stderr 另行保存，不以整理後報告取代原始證據。

#### 15.6.2 紀錄格式

`wire.ndjson` 採獨立的 `bridge.v1` schema；使用與 WAL 相近的欄位名稱，但不假裝是產品 WAL。

| 欄位 | 定義 |
|---|---|
| `schema_version`、`run_id`、`case_id` | 證據版本及本次執行／案例身分 |
| `seq` | bridge 自有單調序號，與 WAL `seq` 無數值關係 |
| `mono_ts_ns`、`wall_ts`、`clock_id` | bridge 觀測時間與時鐘範圍；不可直接与另一程序的單調時間相減 |
| `port_id`、`endpoint_generation` | 邏輯受測埠與外部端點世代，不等同 daemon `conn_epoch` |
| `dut_generation` | DUT 程序／邏輯重啟世代；DUT 重啟不必然改變端點世代 |
| `dir` | 以 serialwrap 視角定義的 `TX`、`RX`、`META` |
| `observation` | 實際觀測位置，見下表 |
| `stream_offset` | 該端點世代、方向與觀測位置內的累計 byte offset |
| `len`、`payload_b64`、`crc32` | 本紀錄實際觀測或確認寫入的 bytes；CRC 變體與 golden vectors 須凍結 |
| `source`、`cmd_id` | 原始 bridge 紀錄固定為外部觀測來源；未知 `cmd_id` 為 null，不猜測 |
| `loss_flag`、`meta` | 證據缺口、錯誤、控制 action ID 或完成狀態 |

必要的觀測位置：

| `observation` | `dir` | 代表的證據 |
|---|---|---|
| `serial_read` | TX | bridge 已從虛擬埠對端讀到 serialwrap 的 bytes |
| `dut_stdin_write` | TX | bridge 對 DUT 連接程序 stdin 的寫入已完成；不代表 DUT 應用程式已處理 |
| `dut_stdout_read` | RX | bridge 已從 DUT 連接程序 stdout 讀到 bytes |
| `serial_write` | RX | bridge 對虛擬埠對端的寫入已完成；不代表 serialwrap 已讀取或寫入 WAL |

寫入失敗且無法確定接受長度時，記錄不確定區間與錯誤，不得把「要求寫入的長度」填成「成功寫入長度」。

刻意丟棄資料的 fault injection 應記錄在 control／fault metadata。`loss_flag` 用來標示擷取證據不完整，不應把「情境設計的 drop」與「錄證工具漏掉資料」混成同一件事。

#### 15.6.3 對照規則

乾淨、無刻意斷線的案例，在宣告的 drain／settle 完成後，以每個埠、方向與端點世代的連續 byte stream 比對：

```text
serialwrap WAL 的 TX bytes
    ↔ bridge serial_read 的 TX bytes

bridge serial_write 的 RX bytes
    ↔ serialwrap WAL 的 RX bytes
```

**不得要求兩邊 chunk 邊界、NDJSON 筆數、seq 或時間戳相同。**一筆 WAL 可對應多筆 bridge 紀錄，反之亦然。

先驗證原始 bytes、長度、CRC 與完整性，再進行必要的語意正規化。動態 `cmd_id`、nonce、時間戳只能使用事先核准的映射規則；不得用廣泛 regex 抹掉真正差異。

`correlation.ndjson` 以測試協調器建立的 logical operation ID、CLI 回應、指定 marker 與 byte offset 建立對應。daemon 自述的 `cmd_id`／epoch 只是關聯資料，不是獨立外部事實。

跨方向採必要的因果關係與時間區間比對，不建立不存在的全域 UART byte 總順序。

#### 15.6.4 證據能力限制

TX WAL 表示產品紀錄了 OS 接受寫入；bridge 的 `serial_read` 才表示對端已觀測到 bytes。DUT 是否執行命令，仍需 DUT 回覆、狀態或指定副作用證據。

UART write 與 WAL append 之間存在不可忽略的 crash window。故障案例須描述可接受的不確定範圍；不能要求所有情況一律完全相等，也不能將所有差異概括成「正常 crash」。

零 TX assertion 須具備健康的擷取器、明確觀測窗口，以及請求結束／閘門穩定的 barrier。多來源同時發送且無法歸因時，不得只憑一段安靜時間宣稱某請求無副作用。

原始證據可能包含登入資料。L1.5 預設使用合成帳密；實機證據需限制存取。對外報告使用遮罩衍生檔，原始與遮罩版本各自保留 hash，不得遮罩後仍宣稱保有完整 byte 等價證據。

### 15.7 情境腳本與 Docker DUT 模擬器

#### 15.7.1 情境執行模型

沿用 `func-test` 的 YAML 調度概念，新增有限、可稽核的動作，不另設通用 workflow 引擎。

每個情境具有固定 ID、revision、seed、前置能力、觸發條件、timeout、故障動作與預期不變量。

固定 seed 只固定測試端選擇，不保證 OS 排程完全相同。關鍵動作應以「已收到指定 TX marker」「某 CLI 請求已完成」「DUT 已確認重啟」等事件定位，不依賴一串固定 sleep 假裝決定性。

| 情境 | 必須明確區分的動作 | 主要 assertion |
|---|---|---|
| 延遲回覆 | DUT 繼續讀取，但延後輸出 | timeout、取消、後续可恢復 |
| 半段 prompt | 輸出前段後等待，再輸出後段；插入 ANSI／雜訊 | 不得提早完成；分塊不影響結果 |
| DUT 靜默 | 收命令但不回覆 | Stall／timeout 與錯誤分類 |
| 慢速／不讀取對端 | 限制或暫停 DUT stdin 消費，讓有界緩衝逐步形成背壓 | 不無界增長、不死鎖；不得誤當單純延遲回覆 |
| DUT reboot、埠存活 | 重設 DUT 狀態或重啟子程序，但保留虛擬埠 | Quiet Gate、DeferredBuffer 失效、重新登入 |
| 傳輸斷線 | 關閉／移除經能力確認的虛擬端點 | Drain、中止、重新開埠與世代隔離 |
| 同板多來源競爭 | 多 CLI／Console 同時提交，不使用測試端 per-board lock 保護 broker | 仲裁、zero-TX、背壓及禁止交錯 |
| 舊事件延遲 | 在交接前後製造延遲輸入／輸出 | 舊上下文不得恢復寫入權；證據不能被靜默丟棄 |
| WAL 干擾 | rotate、reset、外部 unlink、外部 reader 持有 handle | WAL 活性、缺口紀錄與失敗診斷 |

外部工具無法穩定製造的 OS 內部狀態，例如特定長度的 partial write completion，應由 L1 PAL fault test 決定性覆蓋，再用 L1.5／L2 補真實路徑證據。不得把「少回幾個 bytes」冒充「serial backend 部分寫入」。

#### 15.7.2 Docker 執行規則

DUT 容器經 binary stdin/stdout 連接；使用 `-i`，不配置 `-t`。Docker 的 `-i` 保持 stdin 開啟，`-t` 會另配置 pseudo-TTY；正式 byte-stream 案例不加入這層終端機語意。:chatgpt-content-reference{index="7"}

模擬器 stdout 只承載 DUT bytes；診斷走 stderr，且不得將兩者合併。需要測 shell echo、Ctrl-C 或行編輯時，應在 DUT 模型內明確建立終端機情境，不能讓 Docker 的 TTY 設定成為隱藏條件。

容器 image、scenario 與工具版本須固定；禁止測試途中自動升級相依套件。DUT 容器不掛載實體 UART，不掛载 Docker socket，不使用 privileged 模式；預設不需要網路。

「DUT reboot」與「容器／Docker 連線故障」須分別報告。前者應維持 UART transport 存活；後者是否造成虛擬序列埠消失，須由 bridge 的明確 fault 動作控制，不可自動混為一談。

沒有 Docker 的合格 Windows runner，可執行同版本的 stdin/stdout DUT 子程序，標記為 `local-process` 模式。功能案例可共用，但 Docker 拓樸驗收不能因此宣告通過。

### 15.8 Windows 原生對應與覆蓋缺口

#### 15.8.1 兩種 com0com pair 不得混用

Windows L1.5 拓樸為：

```text
Rust／Python serialwrap.exe
        │ 原生 Serial backend
        ▼
       COM-A ⇄ COM-B
                  │
          Windows bridge
                  │
          DUT stdin/stdout
```

com0com 提供雙向虛擬 COM pair，適合讓一端程式模擬裝置、另一端程式按正常 COM 介面存取。:chatgpt-content-reference{index="8"}

此處是 **DUT-side pair**。第 7 章供 Tera Term Serial 使用的是 **Console-side pair**；需要同時測兩者時應使用兩組不同 pair，並在 manifest 明列角色，避免自迴路。

#### 15.8.2 裝置發現與安全邊界

產品一般裝置列舉仍排除 com0com Console 裝置。為測 DUT-side pair，須增加經核准的、僅作用於隔離測試實例的明確裝置 allowlist，或沿用既有可覆寫 device source 的等價機制。

此機制只能影響「發現／選取哪個裝置」，不得替換 serial backend、關閉獨占鎖或繞過 broker。不得為測試把全域 com0com 排除規則直接移除。

OD-11 已核准（附限制）：僅對隔離實例生效且正式設定遇到即拒絕、只接受明確列出的 COM 名稱、不關閉獨占鎖、不更換 serial backend、啟用時 `daemon status` 必須顯示。實際設定名稱於 Stage 0 實作時納入第 9 章，本文不宣稱該設定目前已存在。

#### 15.8.3 驅動资格與斷線語意

須記錄 Windows build、driver 版本與 hash、Secure Boot、HVCI、安裝方式及功能自測結果。「有簽章」不能替代在 memory integrity 啟用環境中的功能驗證。:chatgpt-content-reference{index="9"}

關閉 COM-B 不必然等同移除 COM-A。只有實測確認會產生指定消失／錯誤行為的操作，才能計入 `transport_disconnect` 能力；否則只能稱為 peer close／停止輸入。

真正 PnP 移除、USB 拔插、DTR／RTS 電氣效果與特定驅動取消語意仍由 L2／L3 承擔。

Windows 原生 serial gate 不能因 com0com 不可部署而省略；依 OD-08，退路為直接接 Windows 的實體 USB-UART。Stage 0 須明列受測端、bridge 對端、連接方式、故障能力及逐案例承接關係；仍使用獨立 DUT 模擬器與外部 byte oracle 的案例，登記為 L1.5 實體傳輸替代模式，與真實 DUT 的 L2 案例分開計數。§11.1 的 L1.5 名稱統一為『外部序列埠端到端』，包含虛擬配對及 OD-08 核准的實體替代；其他驅動須另行核准，FakeSerialPair 只補邏輯覆蓋。

#### 15.8.4 Windows-native 案例

依提供的現況，既有 L2／L3 在 Windows 主要只有 `f9-brcm-spaced-prompt-reboot-login` 可執行，其餘大量依賴 tmux／minicom／PTY。不得將這種 SKIP 結果視為 Windows 回歸完成。

Stage 0 起補足以下能力，優先改前端／平台 adapter，保留原有案例目的：

| 領域 | Windows 對應 |
|---|---|
| Human／Agent／多 Console | 以 socket Telnet client 取代 tmux＋minicom；測 Raw ownership、Line-buffer、suspend／resume、deferred input |
| 原生 serial | COM pair 上的開啟、獨占、收送、取消與關閉；能力不足者轉入實體 bench |
| WAL | 實際端點防毒（代表機台為 第三方企業端點防毒（EDR）；Windows Defender 關閉）開啟下跨 rotate、外部 unlink、外部 reader 持有 handle 及活性檢查 |
| `windows-task` | 登入觸發、失敗重啟、設定檢查與實際跨過 72 小時 |
| `windows-service` | 採用時另驗證 Session 0、授權、路徑與一般工程師操作 |
| `lr-mixed` | 同一引擎，Human frontend 改 Telnet，行程／RSS 觀測使用 Windows adapter |

Task Scheduler 預設有 72 小時執行限制；`PT0S` 表示無限期。因此 48 小時長跑不能取代 >72 小時監管驗證。:chatgpt-content-reference{index="10"}

### 15.9 Python→Rust Sequential A/B 驗證

A/B 以相同測試版本與情境比較不同受測 artifact，不使用同一 daemon 同時旁接兩套實作。

#### 15.9.1 L1.5 執行程序

1. 凍結契約、案例、工具、DUT image、seed 與平台設定。
2. 啟動新的模擬器實例 A，接 Python 基線，執行並保存證據。
3. 停止 A 的 daemon，確認程序、handle、鎖及相關子程序釋放；釋放不明時立即停止，不開始 B。
4. 建立全新、相同初始條件的模擬器實例 B，接 Rust 候選 artifact。
5. 執行相同情境，先各自驗證絕對契約，再作 A/B 差分。

「各接一份相同模擬器」指相同 revision 與乾淨初始狀態，不是兩個 daemon 同時讀取同一條 UART，也不是把 Python 跑過後的 DUT 狀態留給 Rust。

Stage 1 比較的是：

```text
Python CLI＋Python daemon
        對照
Rust CLI＋相同基線 Python daemon
```

Stage 2 則比較基線 Python daemon 與 Rust daemon；CLI 組合須在 manifest 固定。

#### 15.9.2 實機 A/B

L2／L3 必須使用相同 bench、板子、韌體、profile、串口設定與案例版本，依序取得獨占。切換前完成正常退出及必要 DUT 初始狀態恢復；不可只改 testbed 執行檔路徑便沿用前一輪殘留狀態。

比較至少包含命令結果、外部 marker／輸出、WAL 完整性、卡態、錯誤率與有效吞吐。對有明顯時間漂移的結果，可增加 B/A 複驗，但不得以挑選較好的一次取代預先定義的判定規則。

Python 基線不是正確性的唯一 oracle。Rust 與 Python 一起違反安全不變量，仍為 FAIL；已核准 BC 與缺陷修復則依明列差異比較，不要求複製錯誤行為。

### 15.10 L2 實機回歸與既有護欄

`serialwrap_regression` 保持為既有 bug 回歸套件，不因新增 bridge 而被降級或取代。

**紅燈哨兵：**`f4-background-result-tail-consistent` 維持 #159 對應。Rust 轉綠須由外部結果證實缺陷修復，記錄行為變更；不得放宽 tail 一致性 oracle。

**U-Boot 唯讀護欄：**F9 只允許中斷 autoboot、白名單唯讀命令與 `reset`／`boot`。禁止 `saveenv`、`setenv`、flash 寫入、`tftpboot` 與命令串接。每個 F9 case 收尾須驗證回到 READY；收尾失敗不得只記清理警告。

**分診：**FAIL 保留 `category`＋`reason_code`。板缺工具、缺指定 banner 等已確認能力不足可 SKIP；但必要驗收能力缺失仍阻擋相應平台出口。

**Preflight：**版本、bench lock、目標板身分與受測執行檔路徑均須通過；不得有殘留 throwaway daemon，同一 live bench 不得並行 pytest。

**F12 WAL 活性修訂：**BC-01 常駐寫入 handle 與 Windows mtime 的更新時機不相容，必須改以讀到新且可解析的 WAL 紀錄／seq／指定 marker 為核心 oracle；mtime 可保留為診斷資訊。這是明確核准的 oracle 修訂，不得為了通過測試而恢復每筆關檔，或僅人工更新 timestamp。:chatgpt-content-reference{index="11"}

新增 bug case 仍先判斷最低足夠層級：純邏輯進 L1；需跨程序、真 serial backend 與外部 bytes 者進 L1.5；只有實機才可重現者進 L2。PR 須記錄選層理由。

### 15.11 L3 穩定性與 48 小時長跑

#### 15.11.1 保留既有 `lr-mixed`

既有工作負載維持：

4 個 agent worker 輪流操作兩塊板，混合 line／background／interactive，每動作具有唯一 marker，並使用 per-board lock；另有 1 個模擬 Human，每 2～5 分鐘輸入 `HUMAN_TICK`；snapshot thread 每 300 秒記錄 session、RSS 與 PID。

Linux Human frontend 保留 tmux／minicom；Windows 使用 Telnet frontend。前端差異不得改变核心工作負載、案例 oracle 或結果分類。

per-board lock 維持基線可比性，不擅自移除。它限制了同板同時提交競爭，因此不得宣稱 `lr-mixed` 已充分驗證 broker admission／仲裁競態；該項由 L1.5 的無測試端序列化競爭案例補足。

#### 15.11.2 故障保留與監管測試分離

保留既有行為：case 級異常記事件後續跑；重大事件，例如 daemon PID 消失或两板同時非 READY 超過 15 分鐘，停止負載並保留現場，不由測試引擎重啟 daemon。

**`lr-mixed` 的故障保留模式不得被外部監管器自動重啟抵銷。**長跑使用隔離的測試實例，明確關閉該實例的自動重啟，或提供等價且可稽核的故障保留措施；Python／Rust A/B 使用相同設定。

`windows-task`／systemd 的自動重啟能力另以監管案例驗證。刻意 kill 後重啟可以是監管測試的預期結果，但不能拿來宣稱 daemon 本身沒有 crash。

Canary 若使用生產監管設定，任何非預期 PID／程序世代變更仍須計為事故，不能因服務已自動恢復就消失。

#### 15.11.3 產出與觀測完整性

保留：

`snapshots.ndjson`、`events.ndjson`、`longrun-analysis.md`。

分析保留 per-source 命令計數、`stuck_attached` 區段、`pid_changes`、`daemon_death_at`、RSS 首尾趨勢，並增加或由外層報告補足：

有效運行時長、每板與每來源的 submitted／accepted／completed／failed／unresolved、預期拒絕與非預期錯誤、有效吞吐、最長卡態、測試 worker 存活情況、觀測缺口與證據 hash。

300 秒 snapshot 不足以提供秒級精確卡態時長。只能依實際觀測回報上下界；接近 gate 邊界時須提高獨立觀測精度或判為證據不足，不得產生虛假的精確秒數。

新增觀測不得使用會延長 Lease 的 `interactive-status` 來「監看」租約，避免觀測本身改變受測行為。

#### 15.11.4 原有 FAIL 與發版閘門

保留既有引擎 FAIL 判準：

重大事件、`daemon_death_at` 非空、單板連續非 READY ≥15 分鐘，或 submit ≥10 時命令錯誤率 >50%。

**這些是既有引擎的 FAIL 條件，不代表其餘結果均足以發版。**整合報告須同時保留 `engine_result` 與 `release_gate_result`。

發版長跑至少還須满足：

| 面向 | 必要條件 |
|---|---|
| 完整性 | 完成連續 48 小時指定工作負載；不得拼接短 segments 湊時數 |
| 安全 | 未授權 TX、禁止的雙重持有、跨上下文回放及危險自動重送事件為零 |
| 存活 | 無非預期 daemon 結束、重啟或程序世代切換 |
| 負載有效性 | 每板、每來源確實持續工作；不能靠停止提交而降低錯誤率 |
| 結果完整性 | 未結案命令不得從分母消失；預期拒絕与非預期錯誤分開統計 |
| 資源 | RSS 等指標符合事先核准的預算與趨勢限制，不僅比較首尾兩個值 |
| 相對基線 | 錯誤率、卡態與有效吞吐符合事先凍結的 Python→Rust 不退化準則 |
| 證據 | 無未說明的觀測缺口；工具故障不得判產品 PASS |

發版門檻唯一來源為 `realhw/acceptance/longrun_acceptance.yaml`，由既有測試引擎之外層 gate 判定器讀取；報告須保存其 revision、hash 與實際生效內容，不得由其他前端維護另一份數值。以下前三項依 OD-09 已凍結，第四項依 §14.2 的期限凍結；檔案建立及判定器實作不等於驗收已通過：

- 非預期命令錯誤率不超過 1%，且不高於對應 Python 基線。
- 同負載 48 小時下，`stuck_attached` 總時長與最長連續區段不高於基線。
- 每板、每來源的有效完成吞吐不低於基線的 95%。
- 資源預算、容許差異、必要樣本量與統計不確定性處理方式，在執行前凍結。

上述百分比已依 OD-09 凍結，但不是由既有 32 小時資料推算出的已驗證 SLO。不得看完候選版本結果才修改門檻。

既有產品缺陷可保留明確、有期限的核准例外紀錄，但該紀錄不豁免 OD-09 的發版門檻，也不得將不合格的 `engine_result` 或 `release_gate_result` 改為 PASS。若需變更非安全門檻，須由擁有者新增裁決並於新一輪驗收前凍結；既有失敗結果保留。安全不變量不得豁免。

#### 15.11.5 Python 基線

既有 32 小時、31 segments、49,899 submitted 的結果保留為歷史診斷證據，主要退化模式為卡在 ATTACHED 未回 READY。

正式比較須另外建立相同測試引擎、相同負載與平台的連續 48 小時基線。BC-01 前後若要做效能／穩定性比較，兩側測試版本與量測口徑須一致，已核准行為差異另列。

Linux 與 Windows 基線分開，不合併平均。Rust 結果同樣逐板、逐來源、逐平台比較，不以總平均掩蓋某一平台或來源退化。

### 15.12 最小交付物與完成判定

本章最小交付物為：

1. 可獨立執行並通過自測的 bridge、stdin/stdout DUT adapter、固定版本 image 與控制介面。
2. `func-test` 既有案例的模式對應清單，以及必要安全／故障情境。
3. 外部證據 schema、WAL 關聯規則、比較器與完整性檢查。
4. L2／L3 的逐 case 契約與能力清單、Windows adapter、核准的 F12 oracle 修訂。
5. 各平台基線、A/B 報告、長跑 gate 與 Stage 0～3 出口證據索引。

bridge 完成的定義不是「可以 echo」，而是能從 serialwrap 外部保存完整、可核對的證據，並在受測物、模擬器或自身故障時作出正確分類。

L2／L3 完成整合的定義不是「CLI 指到 Rust 可以啟動」，而是既有案例的契約、能力、版本、護欄與報告仍有效，且實際取得對應階段所需的證據。

---

## 附錄：版本沿革

| 版本 | 日期 | 主要變更 |
|---|---|---|
| v1～v2 | 2026-10-14 | Gemini 初版五大決策；主持人對照 repo 查證，修正不存在的模組、WAL 格式、平台 seam 等事實錯誤 |
| v3～v3.2 | 2026-10-14 | Gemini 4 Argon 依 GPT-6 Astra 對抗審查重寫：雙層互斥（實例鎖＋每實體 UART 一個 owner）、Two-Phase Epoch Drain Barrier、命令終態區分、WAL 證據層級；契約詞彙對齊實際 CLI 指令樹與錯誤碼 |
| v4 | 2026-10-14 | 整合完整版；Stage 2 為計畫終態、Stage 1 為退守檢查點；BC-01～05；待擁有者裁決 OD-01～06 |
| v5 | 2026-10-14 | ChatGPT Pro 納入 `uart-stdio-bridge`（L1.5）、TestPilot 實機回歸（L2）與 `realhw` 48 小時長跑（L3）；28 個逐節 patch；OD-07～13 |
| v6 | 2026-10-15 | 擁有者逐項裁決 OD-01～13；BC-01～05 核准；Defender 誤植更正為 第三方企業端點防毒（EDR）；工期依工作包重估 |
| 定稿 | 2026-10-15 | 依 Astra 定稿前審查 27 項與複核 3 項修正；長跑門檻檔定於 `realhw/acceptance/longrun_acceptance.yaml` |
