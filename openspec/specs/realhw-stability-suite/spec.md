# realhw-stability-suite Specification

## Purpose
定義 serialwrap **實機穩定性測試套件**（`python3 -m realhw`）的能力契約：一套獨立、stdlib-only、操作已部署 live daemon 與真實 UART 板卡的 harness（不 import `sw_core`、不進 CI、不入 wheel、`pytest tests/` 不收集），提供 tier 化 case 選擇與執行、部署前 preflight 守門、P0 煙霧與 P1 核心穩定性 case、無人看護長跑與事後分析，用於持續驗證多 agent／多 human console 共用單一 UART 的 broker 在真機上的長期穩定性。

## Requirements
### Requirement: tier 化 case 執行與選擇
套件 SHALL 以 `python3 -m realhw` 執行，支援 `--tier`（`p0`／`p1`／`longrun`，可逗號多選）、`--only <case-id>`、`--skip <case-id>`、`--list`、`--report-dir`、`--duration`（僅 longrun，預設 32h，接受 `<N>h/<N>m/<N>s`）。`longrun` tier MUST NOT 被 `p0`/`p1` 隱含，必須顯式指定。Case SHALL 固定順序、彼此獨立、continue-on-failure（單 case FAIL 記錄後續跑）。套件 MUST NOT 被 `pytest tests/` 收集、MUST NOT 打包進 wheel。

#### Scenario: 部署後標準執行
- **WHEN** 執行 `python3 -m realhw --tier p0,p1`
- **THEN** 依序執行 P0 與 P1 全部 case，任一 FAIL 不中止後續 case，長跑不被執行

#### Scenario: 長跑獨立指定與排除
- **WHEN** 執行 `python3 -m realhw --tier longrun --duration 2h`
- **THEN** 僅執行長跑 2 小時；`--tier p0,p1` 的執行則完全不含長跑

### Requirement: preflight 守門
套件 SHALL 於任何 case 執行前完成 preflight，任一項不過 MUST 拒跑整場並列出缺項：(1) 部署新鮮度（git 比對 origin/main 落後即警告、記錄 `serialwrap --version`）；(2) `serialwrap doctor` 通過；(3) 兩板 READY 且 by-id serial 與組態相符；(4) `tmux`、`usbipd.exe`、`sudo -n` 可用；(5) 環境乾淨（無殘留 throwaway daemon、無其他 pytest 行程、live state.json 無污染哨兵）。preflight SHALL 印出本輪將執行的破壞性動作清單（板卡 reboot／daemon restart／usbipd 插拔）。

#### Scenario: 環境不滿足即拒跑
- **WHEN** tmux 不可用或兩板未全 READY 時執行套件
- **THEN** 不執行任何 case，輸出缺項清單，exit code 非零

#### Scenario: 與單元測試互斥
- **WHEN** preflight 偵測到同機有 `pytest` 行程執行中
- **THEN** 拒跑並說明互斥原因（#120 live guard 會把本套件對 live 的操作判為 FAIL）

### Requirement: 測試部署後系統
所有 case 的驅動 SHALL 經由已安裝的 `serialwrap` CLI（subprocess）操作 live daemon 與真板；套件 MUST NOT import `sw_core`、MUST NOT 直接修改 daemon 的 state/config 檔案。破壞性 case SHALL 帶 `destructive` 標記並自行還原（release 後收回、reboot 後等 READY）；harness SHALL 於 case 之間驗證兩板 READY，不 READY 時嘗試恢復一次，仍失敗則後續依賴板卡的 case 記 SKIP。

#### Scenario: case 弄髒環境不擴散
- **WHEN** 某 destructive case 結束後板卡未回 READY
- **THEN** harness 嘗試一次恢復（如 `device attach`／等待），仍失敗則後續依賴 case 記 SKIP 而非 FAIL，報告載明起因 case

### Requirement: 報告與 evidence
每輪執行 SHALL 產出 `report.json`（run metadata＋逐 case verdict/reason/duration/evidence 路徑）與 `report.md`（摘要表＋失敗案例診斷）至 `~/b-log/realhw-reports/<timestamp>/`（`--report-dir` 可覆寫）；每 case 的 evidence（執行命令與輸出、tmux capture-pane 快照、相關檔案引用）SHALL 落於該 case 子目錄。FAIL 的失敗訊息 SHALL 附上該 case 宣告的診斷提示（歷來已知坑）。

#### Scenario: 失敗自帶診斷
- **WHEN** `p0-cmd-async` 因 status 取回空 stdout 而 FAIL
- **THEN** report.md 該條目附診斷提示（如「submit 後立刻讀有 line race，隔一拍再讀」）與 evidence 連結

### Requirement: 長跑無人看護與事後分析
長跑 SHALL 以 4 個 agent worker（兩板輪流、line/background/interactive 混合、唯一 marker）＋1 個模擬 human（tmux+minicom 週期輸入）持續負載，並每 5 分鐘快照 `session list`／`daemon status`／daemon RSS。case 級異常 SHALL 記錄後繼續；daemon 死亡或兩板同時長時間非 READY SHALL 視為重大事件——停止負載、保留現場、MUST NOT 自動重啟 daemon。結束（時間到或 SIGINT）SHALL 產出 `longrun-analysis.md`：狀態轉換時間線、各 source 命令 submitted/done/error 統計、卡 ATTACHED 事件清單、資源趨勢。

#### Scenario: 無人環境重大事件保留現場
- **WHEN** 長跑第 20 小時 daemon 行程消失
- **THEN** 負載停止、不重啟 daemon，快照與 log 保留，分析報告標記事件時間點與前後狀態

#### Scenario: SIGINT 提前結束仍出報告
- **WHEN** 長跑執行中收到 SIGINT
- **THEN** 停止負載並以既有資料產出完整 longrun-analysis.md

### Requirement: 人可讀 checklist 文件
repo SHALL 提供 `docs/func-test/realhw-stability-checklist.md`：P0/P1 逐 case 對照 case id 與手動等效命令、P2 手動程序（MCU flash、U-Boot、self-test 全譜、安裝轉換、Windows）、前置作業（部署／環境清潔／throwaway 隔離通則）與坑一覽。套件 `--list` 輸出的 case id SHALL 與 checklist 一致。

#### Scenario: 手動 fallback
- **WHEN** 自動化不可用（如 tmux 缺失）而需人工驗證
- **THEN** checklist 提供每條 P0/P1 case 的手動等效命令與判定標準

