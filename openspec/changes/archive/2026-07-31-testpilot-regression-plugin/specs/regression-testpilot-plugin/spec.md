# regression-testpilot-plugin

## ADDED Requirements

### Requirement: plugin 註冊與 dev-only 安裝
回歸套件 SHALL 以獨立 distribution `serialwrap-regression`（目錄 `regression/`，package `serialwrap_regression`）提供 testpilot plugin，經 entry-point `testpilot.plugins` 註冊名稱 `serialwrap_regression`；安裝 SHALL 為 dev-only editable（testpilot venv `pip install -e regression/`），release wheel MUST NOT 包含本套件。

#### Scenario: editable 安裝後可被發現
- **WHEN** 於 testpilot venv 執行 `pip install -e regression/` 後執行 `testpilot list-plugins`
- **THEN** 清單出現 `serialwrap_regression` 且 case 數 ≥ 全 family 合計數

### Requirement: case registry 與 issue 對照
每個 case SHALL 宣告 `id`、`family`（F1–F10）、`title`、`issues`（對應已 CLOSED 且有實際修正的 issue 編號，至少一個）、`destructive` 標記；registry SHALL 為本 package 自有（MUST NOT 註冊進 realhw 全域 REGISTRY），id 重複 SHALL 拒絕載入。case metadata 與報告 SHALL 帶出 `issues` 供回歸追溯。

#### Scenario: registry 獨立不互染
- **WHEN** 同時載入 realhw registry 與 regression registry
- **THEN** 兩者 case 集合不重疊，reliability plugin 的 discover 不含 regression case，反之亦然

#### Scenario: 回歸可追溯
- **WHEN** 任一 case 出現在報告
- **THEN** 該條目可見對應 issue 編號（如 `#94`）

### Requirement: 分診契約（serialwrap＝受測物）
本 plugin SHALL 視 serialwrap 為受測物：回歸破功 SHALL 標 `category="test"`（FailTest）；bench／板卡／工具問題 SHALL 標 `category="environment"`（FailEnv）；testbed 設定錯誤 SHALL 標 `category="configuration"`（FailConfig）。FAIL 結果 MUST 帶非空 `category` 與 `reason_code`。

#### Scenario: 回歸破功分類為 FailTest
- **WHEN** 某 case 觀測到當初 issue 的錯誤行為再現（如失敗 CLI 的 `error_code` 為空）
- **THEN** 該 case FAIL 且 `category="test"`，testpilot 分診為 FailTest

### Requirement: testpilot 契約整合
plugin SHALL 沿 reliability 實證設定：agent-config 的 remediation `enabled: true`、`max_attempts: 1`、`hooks.enabled_hooks` 含 `on_failure`、MUST NOT 覆寫 decision hooks；`retry.max_attempts` SHALL 顯式設 1；`execute_step` SHALL 一律回 `success=True` 並把判決集中於 `evaluate`。

#### Scenario: FAIL 不變 Inconclusive
- **WHEN** 任一 case 判 FAIL
- **THEN** testpilot 報表分類為 FailTest／FailEnv／FailConfig 之一（非 Inconclusive）

### Requirement: preflight 守門與版本對齊
prepare_run SHALL 執行 preflight：doctor 全綠、testbed 宣告板卡 READY、必要工具存在、殘留 throwaway daemon 偵測、pytest 互斥、benchlock（與 reliability／wifi_llapi 共用；被持有即整場拒跑）、外部 testpilot run 偵測；並 SHALL 比對 pinned serialwrap CLI `--version` 與 daemon 回報版本，不一致 SHALL suite-refuse。PATH 上（如 venv 內）解析到的 serialwrap 版本 SHALL 記入 preflight notes 供診斷（不擋）。

#### Scenario: 版本歪斜整場拒跑
- **WHEN** pinned CLI 回報 0.2.4 而 daemon 回報 0.2.3
- **THEN** prepare_run 拒跑並載明兩版本，不執行任何 case

### Requirement: serialwrap CLI 執行檔 pin 定
所有 case 對 serialwrap 的操作 SHALL 經由 testbed `serialwrap_exe`（預設 `~/.local/bin/serialwrap`）指定的執行檔，MUST NOT 依賴行程 PATH 解析（testpilot venv 內 PATH 會解析到 stale client，#154）。

#### Scenario: venv 內執行不吃 stale client
- **WHEN** plugin 於含 serialwrap 0.2.1 的 venv 內執行且 testbed 未特別設定
- **THEN** 所有 case 實際呼叫 `~/.local/bin/serialwrap`（部署版），非 venv 內 0.2.1

### Requirement: destructive gate 與執行順序
testbed SHALL 提供 `allow_destructive`（預設 false）；false 時 destructive case（F9 reboot、F10 throwaway 交接）SHALL 記 SKIP（MUST NOT FAIL）。case 執行順序 SHALL 非破壞性 family 在前、破壞性（F9→F10）壓軸；執行策略 SHALL sequential、max_concurrency 1。

#### Scenario: 預設快跑不動板子
- **WHEN** `allow_destructive: false` 下執行全套
- **THEN** F9/F10 case 全 SKIP，其餘照跑，全程無 reboot、無 device release

### Requirement: U-Boot 唯讀護欄
F9 的 U-Boot 互動 SHALL 經由 harness 的 `UBootConsole` API，僅允許：中斷 autoboot、白名單唯讀命令（`printenv`、`bdinfo`、`version`、`help`、`echo`）、以 `boot`／`reset` 離開。`saveenv`、`env save`、`env default`、`setenv`、flash 寫入（`sf write`／`nand write`／`mmc write`）與 `tftpboot` SHALL 一律拒送（raise）。每個 F9 case 收尾 SHALL 等板子回 READY，MUST NOT 把板子留在 U-Boot prompt。

#### Scenario: 禁令命令被 harness 攔截
- **WHEN** case 程式碼嘗試經 `UBootConsole` 送出 `setenv bootdelay 0`
- **THEN** harness 拒送並 raise，該 bytes 不落 UART

#### Scenario: 收尾必回 READY
- **WHEN** F9 case 於 U-Boot prompt 完成驗證
- **THEN** case 以 `boot`／`reset` 離開並等到 session READY 才回報 verdict；逾時則 FAIL 並執行恢復

### Requirement: throwaway daemon 隔離（F10）
帳密解析失敗情境 SHALL 以 throwaway daemon 製造：隔離 `SERIALWRAP_RUN_DIR`／`SERIALWRAP_STATE_DIR`／`SERIALWRAP_WAL_DIR`／`SERIALWRAP_BY_ID_DIR`（sandbox 只放目標線）＋自帶 profile，MUST NOT 修改 prod config／state；進場前經 prod `device release` 交接、離場 SHALL 終止 throwaway daemon、收回裝置並等 READY。

#### Scenario: prod 組態零接觸
- **WHEN** F10 case 全程執行完畢
- **THEN** prod 的 config.yaml／state.json 內容不變，板卡回 READY

### Requirement: 報告產出
每輪 SHALL 產出 md＋json 報告至 `~/b-log/regression-reports/tp-<timestamp>/`，含 run metadata（部署版本、git、板卡）、逐 case verdict／family／issues／分類／時長、失敗案例的 reason 與 evidence 連結、PASS/FAIL/SKIP 統計。

#### Scenario: 失敗有據可查
- **WHEN** 任一 case FAIL
- **THEN** 報告載明 reason、分類（category/reason_code）、對應 issues 與 evidence 路徑

### Requirement: 回歸 case 覆蓋（10 family）
plugin SHALL 覆蓋 #155 定案的 10 個 family：F1 命令契約、F2 背壓、F3 失敗可觀測性、F4 狀態語義、F5 console 共存、F6 RPC 不凍結、F7 檔案傳輸、F8 daemon 單一性、F9 開機/U-Boot、F10 登入帳密；每 family 至少一個 case，oracle 一律為「對應 issue 當初的錯誤行為不得再現」。板卡能力不具備（如 COM1 無 U-Boot banner、板缺 `base64`）SHALL SKIP 而非 FAIL。

#### Scenario: 能力缺失記 SKIP
- **WHEN** F7 case 發現 bcm 板無 `base64` 工具
- **THEN** 該 case 記 SKIP 並載明缺失能力，不記 FAIL

### Requirement: 文件與回歸 case 政策
README SHALL 新增「TestPilot 回歸測試」中英雙語章節（安裝、執行、destructive gate、何時跑）；`docs/regression-plugin.md` SHALL 載 family↔issue 對照與「從新修好的 bug 新增 case」SOP；CLAUDE.md SHALL 載條件式政策——修復 bug issue 時必須評估回歸測試歸屬（pytest 可覆蓋→pytest；需實機才驗得到→必加 regression case 並掛 issue 編號），PR 描述須記錄評估結論；PR template checklist SHALL 含對應勾選項。

#### Scenario: 修 bug 的 PR 有評估記錄
- **WHEN** 一個修 bug issue 的 PR 送審
- **THEN** PR 描述含回歸 case 評估結論（新增了哪個 case，或 pytest 已覆蓋／為何免加）
