# realhw-stability-suite（delta）

## MODIFIED Requirements

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

## ADDED Requirements

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
