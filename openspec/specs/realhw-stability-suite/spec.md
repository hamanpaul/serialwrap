# realhw-stability-suite Specification

## Purpose
定義 serialwrap **實機穩定性測試套件**（`python3 -m realhw`）的能力契約：一套獨立、stdlib-only、操作已部署 live daemon 與真實 UART 板卡的 harness（不 import `sw_core`、不進 CI、不入 wheel、`pytest tests/` 不收集），提供 tier 化 case 選擇與執行、部署前 preflight 守門、P0 煙霧與 P1 核心穩定性 case、無人看護長跑與事後分析，用於持續驗證多 agent／多 human console 共用單一 UART 的 broker 在真機上的長期穩定性。
## Requirements
### Requirement: tier 化 case 執行與選擇
套件 SHALL 以 `python3 -m realhw` 執行，支援 `--tier`（`p0`／`p1`／`remote`／`longrun`，可逗號多選）、`--only <case-id>`、`--skip <case-id>`、`--list`、`--report-dir`、`--duration`（僅 longrun，預設 32h，接受 `<N>h/<N>m/<N>s`）。`longrun` 與 `remote` tier MUST NOT 被 `p0`/`p1` 隱含，必須顯式指定。Case SHALL 固定順序、彼此獨立、continue-on-failure（單 case FAIL 記錄後續跑）。套件 MUST NOT 被 `pytest tests/` 收集、MUST NOT 打包進 wheel。

#### Scenario: 部署後標準執行
- **WHEN** 執行 `python3 -m realhw --tier p0,p1`
- **THEN** 依序執行 P0 與 P1 全部 case，任一 FAIL 不中止後續 case，長跑與 remote 族不被執行

#### Scenario: 長跑獨立指定與排除
- **WHEN** 執行 `python3 -m realhw --tier longrun --duration 2h`
- **THEN** 僅執行長跑 2 小時；`--tier p0,p1` 的執行則完全不含長跑

#### Scenario: remote 族獨立指定
- **WHEN** 執行 `python3 -m realhw --tier remote`
- **THEN** 僅執行 remote 族 7 case（rm-topo ×4＋rm-live ×3）

### Requirement: preflight 守門
套件 SHALL 於任何 case 執行前完成 preflight，並區分**兩級判決**：**suite-refuse** 檢查任一不過 MUST 拒跑整場並列出缺項——(1) 部署新鮮度（git 比對 origin/main 落後即警告、記錄 `serialwrap --version`）；(2) `serialwrap doctor` 通過；(3) 兩板 READY 且 by-id serial 與組態相符；(4) `tmux`、`usbipd.exe`、`sudo -n` 可用；(5) 環境乾淨（無殘留 throwaway daemon、無其他 pytest 行程、live state.json 無污染哨兵）；(6) `benchlock`——取得 `~/.local/state/serialwrap/bench.lock` flock 且 pgrep 無進行中的外部 `testpilot run`，拿不到即拒跑。**family-gate** 檢查（`capabilities`）缺項 MUST NOT 擋整場，只使宣告對應 `requires` 的 case 執行期 SKIP 並標 `FailEnv` reason：`remote` 子命令存在性（`remote_capability`）、部署版本 ≥0.2.3（`deployed_daemon_stale`）、docker 可達（`docker_unavailable`）。另 SHALL 以 `WinSwCli` 探測 Windows 端 serialwrapd（存在與裝置持有清單烙進 run meta）；Windows 端持有目標 busid 時，兩板 READY 的缺項訊息 SHALL 歸因為 `windows_daemon_holds_device`。preflight SHALL 印出本輪將執行的破壞性動作清單（板卡 reboot／daemon restart／usbipd 插拔）。

#### Scenario: 環境不滿足即拒跑
- **WHEN** tmux 不可用或兩板未全 READY 時執行套件
- **THEN** 不執行任何 case，輸出缺項清單，exit code 非零

#### Scenario: 與單元測試互斥
- **WHEN** preflight 偵測到同機有 `pytest` 行程執行中
- **THEN** 拒跑並說明互斥原因（#120 live guard 會把本套件對 live 的操作判為 FAIL）

#### Scenario: benchlock 互斥
- **WHEN** wifi_llapi 的 `testpilot run` 進行中（或 bench.lock 被他者持有）時執行套件
- **THEN** 整場拒跑並說明互斥來源

#### Scenario: 能力缺項只擋對應族
- **WHEN** 部署 CLI 無 `remote` 子命令但其餘檢查通過
- **THEN** p0/p1 照跑；rm-live 族 case 執行期 SKIP＝`FailEnv/remote_capability_missing`，整場不拒跑

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

### Requirement: 結果分類（category／reason_code）
`CaseResult` SHALL 增 `category`（`environment`｜`session`｜`configuration`｜`test`｜空）與 `reason_code`（自由字串）欄位，向後相容（不填合法）。分類裁決線：case 內斷言失敗預設 `test`（板卡健康由 preflight 與 case 間恢復檢查保證）；case 明確偵測之外因（Windows 端持有、docker/sshd 缺、usbipd 裝置消失）標 `environment`；bench 組態錯誤標 `configuration`；未捕捉例外由 harness 兜底 FAIL 且 category 留空（對應 Inconclusive）＋`reason_code=uncaught_exception`；執行期 SKIP 標 `environment`（如 `base64_missing`、`broken_by:<case-id>`）。report.json／report.md SHALL 呈現分類欄。

#### Scenario: 既有 SKIP 分類化
- **WHEN** `p1-cmd-file` 因 target 缺 base64 而 SKIP
- **THEN** CaseResult 帶 `category="environment"`、`reason_code="base64_missing"`，報表分類欄如實呈現

### Requirement: remote 隧道實機驗證（tier `remote`）
套件 SHALL 提供 7 個 remote case：`rm-topo-direct`／`rm-topo-nat-host`／`rm-topo-dual-nat`／`rm-topo-gwports` SHALL 逐拓樸包裝 `tools/docker/remote_tunnel_test.sh`（該 script SHALL 增加逐拓樸分派參數），以 exit code＋log 尾段產 verdict 與 evidence（容器封閉世界、驗工具鏈）；`rm-live-e2e`／`rm-live-orphan`／`rm-live-cycle` SHALL 以 docker 容器為 ssh 對端、對**部署 daemon＋真板**驗證：`-R` expose 後容器內 agent 穿隧道執行 `session list` 與 `cmd submit`（真板回 marker、WAL source 歸因正確）、`kill -9` ssh 後 `remote status` prune 自癒、連續 open/close ×5 registry 不累積；rm-live 全程 SHALL 斷言 live daemon pid 不變、結尾 `remote close all` 後 state dir 淨空。requires：rm-topo→`docker`；rm-live→`docker`＋`two_boards`＋`remote_capability`。全族非破壞性。image 建置 SHALL 延遲到第一個 rm-topo case 並以 build log 為 evidence。rm-live 與 rm-topo SHALL 使用不同容器名前綴且各自 teardown（finally 掃殘留容器/network）。

#### Scenario: 穿隧道端到端
- **WHEN** `rm-live-e2e` 開 `-R` expose 至容器後於容器內 `cmd submit --selector COM0 --cmd "echo <marker>"`
- **THEN** 真板回 marker、WAL 記錄該命令之 source、close 後 registry/log 淨空、daemon pid 全程不變

#### Scenario: 孤兒隧道自癒
- **WHEN** `rm-live-orphan` 對隧道 ssh 進程 `kill -9` 後執行 `remote status`
- **THEN** 孤兒被 prune、狀態檔清除、隨後重新 open 成功

### Requirement: hp-cycle Windows 端自動救援
`p1-hp-cycle` 於 usbipd attach 回插失敗時 SHALL 執行自動救援鏈：以 `WinSwCli`（經 `/mnt/c/...` 呼叫 Windows 端 serialwrap.exe）探測 Windows 端是否持有目標裝置；持有則 Windows 端 `device release` 後重試 attach（至多 2 次），救援過程記入 evidence；救援成功則 case 照常收尾。救援失敗 SHALL FAIL＝`category="environment"`、`reason_code="windows_daemon_holds_device"` 並標 attended；後續依賴 case 走既有 broken_by SKIP。救援決策 SHALL 為純函式（注入探測結果、回傳動作序列），與 subprocess 執行層分離。

#### Scenario: Windows 端持有自動救回
- **WHEN** hp-cycle 回插時 usbipd attach 失敗且 Windows 端 serialwrapd 持有該裝置
- **THEN** 自動執行 Windows 端 `device release` 後重試 attach 成功，case 收尾 PASS，evidence 含救援紀錄

