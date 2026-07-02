# test-live-isolation

## ADDED Requirements

### Requirement: in-process 測試的 state 路徑可注入
`SessionManager` SHALL 接受建構參數 `state_path`（keyword，default `None`）；提供時所有 state 讀寫（`_load_state`／`_save_state`／`.corrupt` 備份／原子寫入暫存檔）MUST 使用該路徑；未提供時 SHALL fallback 至 `session_manager` 模組層 `STATE_PATH` 全域（於建構時讀取，使既有 `setattr` 測試隔離手法持續有效）。`SerialwrapService` SHALL 透傳同名參數。`WalWriter` 的 `wal_dir` 參數 SHALL 以 None-sentinel 於建構時解析模組層 `WAL_DIR`，MUST NOT 於函式定義時凍結。

#### Scenario: 注入 state_path 後建構不碰模組層路徑
- **WHEN** 以 `SessionManager(..., state_path=<tmp>/state.json)` 建構
- **THEN** state.json 寫入 `<tmp>/state.json`，模組層 `STATE_PATH` 所指路徑不被建立或修改

#### Scenario: 未注入時 fallback 相容既有 setattr 隔離
- **WHEN** 測試先 `setattr(session_manager, "STATE_PATH", <tmp>)` 再以預設參數建構 `SessionManager`
- **THEN** state 讀寫落在 `<tmp>`，與現行 19 檔隔離測試行為一致

#### Scenario: WalWriter default 於建構時解析
- **WHEN** 測試先 `setattr(wal, "WAL_DIR", <tmp>)` 再建構 `WalWriter()`
- **THEN** WAL 目錄建立於 `<tmp>`，而非 import 當下凍結的舊值

### Requirement: pytest suite 對 live 資源零接觸（conftest 強制隔離）
`tests/conftest.py` SHALL 於 module top-level（早於任何測試模組 import）將 `SERIALWRAP_STATE_DIR`／`SERIALWRAP_RUN_DIR`／`SERIALWRAP_WAL_DIR`／`SERIALWRAP_CONFIG_DIR`／`SERIALWRAP_LOG_DIR`／`SERIALWRAP_EVENTS_DIR`／`SERIALWRAP_EVENTS_RUNTIME_DIR` 硬覆寫至 per-run 暫存目錄，並將 `SERIALWRAP_BY_ID_DIR`／`SERIALWRAP_BY_PATH_DIR` 指向空目錄；覆寫 MUST 蓋過外層 shell 既有值。另 SHALL 以 function-scoped autouse fixture 對每個測試 patch `session_manager.STATE_PATH` 至 per-test 暫存路徑。

#### Scenario: 未自行隔離的 in-process 測試不落 live
- **WHEN** 任一測試（含未自行 patch `STATE_PATH` 者）於 pytest 下建構 `SessionManager` 或 `SerialwrapService`
- **THEN** state／WAL／events 寫入皆落在暫存目錄，live XDG 路徑不被建立或修改

#### Scenario: subprocess 測試的自有隔離不受影響
- **WHEN** coexist／e2e 型測試以 `os.environ.copy()` 後覆寫自身 tempdir env 啟動 daemon subprocess
- **THEN** 子行程使用測試自身的 tempdir，conftest 的全域覆寫被測試自身的覆寫取代

### Requirement: live guard——suite 結束斷言 live 資源未被觸碰
`tests/conftest.py` SHALL 於 sessionstart 快照、sessionfinish 比對四個 live 維度，任一 FAIL 時 MUST 使 pytest exit code 非零；判定邏輯 SHALL 為純函式（`tests/liveguard.py`）以便單元測試。live 路徑計算 SHALL 採 XDG 公式（`(XDG_STATE_HOME|~/.local/state)/serialwrap/...`、`(XDG_CONFIG_HOME|~/.config)/serialwrap/config.yaml`）並 MUST 忽略 `SERIALWRAP_*` 隔離變數。判定：(1) live state.json 從不存在變存在、或任何 byte 變更 → FAIL；(2) live WAL `raw.wal.ndjson` 消失或 size 縮小 → FAIL（append 視為 live daemon 合法活動）；外層 shell 原有 `SERIALWRAP_WAL_DIR` 路徑任何變更（含同 size 內容改寫）→ FAIL；(3) live config.yaml 任何 byte 變更 → FAIL；(4) systemd unit `serialwrap`（system scope；systemd-user 機器該維度 SKIP）於 sessionstart 為 active 時：MainPID 變更或轉 inactive → FAIL，且以唯讀 RPC 快照比對任一 session 的 `last_tx_at` 前進、`bridge_generation` 變更、`state` 變更或 session 消失 → FAIL；daemon 不存在或不可達 → 該維度靜默 SKIP。環境變數 `SERIALWRAP_LIVE_GATE=warn` SHALL 將非結構級變更降為警告——降級範圍：Guard 1 非結構性內容變更、Guard 2 size 縮小（rotation 誤報情境）、Guard 4 `last_tx_at`／`bridge_generation`／`state`／session 消失（對真板操作情境）；結構級判定 MUST 不受閥管仍 FAIL——Guard 1 結構性破壞（`released`／`bindings`／`aliases` 任一 key 消失，或內容新出現 `/tmp/sw-`、`test-tpl`、`"test:`、`fake-uart` 污染特徵）、Guard 2 檔案消失、Guard 3 與 shell WAL 維度全部、Guard 4 unit 轉 inactive／MainPID 變更。

#### Scenario: 乾淨覆寫與 released 清空不放行
- **WHEN** suite 期間 live state.json 被改寫為不含污染特徵、但 `released` 段落消失的內容（無論 strict 或 warn 模式）
- **THEN** sessionfinish 判 FAIL，exit code 非零並列印結構化 diff

#### Scenario: live daemon 合法 WAL append 不誤報
- **WHEN** suite 期間 live daemon 因板端 RX 使 live WAL size 增加，state.json byte-identical
- **THEN** guard 通過，不產生 FAIL

#### Scenario: 誤路由 TX 被 daemon 快照抓到
- **WHEN** 某測試的 RPC 誤路由使 live daemon 對真板送出命令（`last_tx_at` 前進），但 state.json 未變
- **THEN** sessionfinish 判 FAIL 並列出變更的 session 欄位

#### Scenario: CI fresh runner 真陽性
- **WHEN** CI 環境無既有 live state.json，且某回歸測試在隔離失效下建立了它
- **THEN** guard 以「從不存在變存在」判 FAIL

### Requirement: unittest runner 的 state 維度安全
既有 8 個未隔離測試檔 SHALL 各自於 setUp／tearDown（或等效 fixture）patch `session_manager.STATE_PATH` 與 `wal.WAL_DIR` 至暫存路徑並還原，使 `python3 -m unittest discover -s tests`（不載入 conftest）跑法下 live state.json 亦不被觸碰。repo 測試政策文件 SHALL 註記 pytest 為唯一具完整隔離防線（conftest env＋live guard）的跑法。

#### Scenario: unittest 直跑單檔不落 live
- **WHEN** 以 `python3 -m unittest tests.test_issue24_heartbeat` 直接執行（無 conftest 防線）
- **THEN** live XDG state.json 不被建立或修改
