# serialwrap-reliability：testpilot-core plugin 設計

> 狀態：已與使用者逐段核可（2026-07-20 brainstorm）。
> 範圍：realhw 擴充（remote 族＋分類增強＋hp 救援鏈）＋ testpilot plugin 殼（dev-only editable dist）。
> 關聯：#122（realhw 套件）、PR #143（`serialwrap remote`）、`~/notes/usbipd-attach-failed-report-0718.md`（雙 daemon root cause）。

## 1. 背景與動機

testpilot-core 是本 lab 的測試框架，wifi_llapi plugin 透過 serialwrap 操作 DUT/STA 真板。serialwrap 因此是 wifi_llapi 全部結果的信任基礎——broker 壞了，其上所有測試結果都不可信。

本設計把 #122 的實機穩定性套件（`python3 -m realhw`，29 cases）接上 testpilot：**同一個 lab、同一套框架、兩個 plugin——wifi_llapi 測 DUT，serialwrap-reliability 測大家都依賴的 broker**。建議工作流閉環：serialwrap 重大進版 → reliability suite 綠 → 才跑 wifi_llapi full run。

同時把 PR #143 新增的 `serialwrap remote`（SSH 隧道 CLI）納入 case 覆蓋：其 docker 三拓樸 harness（`tools/docker/remote_tunnel_test.sh`）是**全容器封閉世界**（容器內 daemon＋`uart_harness.py` 假 UART），不碰部署 daemon 與真板——realhw 補的正是這個縫。

### 受測物反轉（分類的靈魂）

wifi_llapi 視 serialwrap 為*環境*（session 掛掉→FailEnv、修環境重試）；本 plugin 視 serialwrap 為*受測物*——**同一現象、分類相反**（session 該 READY 而沒 READY＝產品缺陷→FailTest）。`environment`/`session` category 只留給 serialwrap 管不到的外因（板卡、線材、Windows 端、docker）。

### 雙 daemon 雙世界（bench 既存暗礁）

這台實體機上有**兩套 serialwrap 部署共享同一棵 USB 樹**：WSL 受測 daemon（systemd）與 Windows 原生 `serialwrapd.exe`。usbipd detach 後裝置回到 Windows，Windows 端 daemon 會抓走 COM port 持有 exclusive handle，Windows 因此拒絕再匯出給 WSL——這是 2026-07-19 hp-cycle 把 COM1 弄到「疑似要人工重插」的真正根因（見 0718 報告）。解法可腳本化：**Windows 端 `serialwrap device release`**（從 WSL 經 `/mnt/c/...` 呼叫），不需人工、不需 reboot。本設計據此新增 preflight 探測與 hp-cycle 自動救援鏈。

## 2. 範圍

**Phase 1（serialwrap repo，realhw 擴充，standalone 可交付）**：
- remote 族 7 cases（新 tier `remote`）。
- `CaseResult` 增 `category`/`reason_code` 欄與逐案分類標註。
- `p1-hp-cycle` 內建 Windows 端自動救援；`WinSwCli` driver。
- preflight 新增：benchlock、windows_daemon 診斷、capabilities（family-gate）。
- `tools/docker/remote_tunnel_test.sh` 加逐拓樸分派參數（微改）。

**Phase 2（同 repo `reliability/`，plugin 殼）**：
- dev-only editable dist：pyproject＋entry point＋`PluginBase` 轉接。
- testbed.yaml loader（與 config.json 雙來源等價）。
- 自訂 reporter（md/json，重用 realhw 報告）。

**明列不做（v2 deferred）**：
- lr-mixed 加 remote worker（隧道 48h 耐久／autossh 重連）。
- `-L` 連真遠端 daemon 的 live 案（`rm-topo-direct` 已含容器側 `-L`）。
- PassAfterRemediation（v1 remediation 動作三重鎖死、僅留 snapshot 通道；恢復靠 realhw 內建 `recovery_command`）。
- wifi_llapi 側共用 benchlock（本案單邊實作，不改 wifi_llapi）。

## 3. 整體架構

```
┌─ 開發機 bench（唯一會裝 plugin 的地方）──────────────────────────────┐
│                                                                      │
│  ~/prj_arc/testpilot (venv)          ~/prj_pri/serialwrap (checkout) │
│  ├─ testpilot-core   (editable)      ├─ pyproject.toml → release wheel│
│  ├─ wifi_llapi       (editable)      │   （不動；不含 realhw/plugin） │
│  └─ serialwrap-reliability (editable)├─ realhw/        ← 引擎（擴充） │
│      └──────── entry_point ──────────┤   harness/drivers/preflight/  │
│                                      │   cases/{p0,p1_*,remote,longrun}│
│  configs/testbed.yaml ← bench 事實源  └─ reliability/   ← 新 dev dist │
│  （boards/busid/serial/usbipd_exe/       └─ serialwrap_reliability/   │
│    win_serialwrap_exe）                    plugin.py / core.py /      │
│                                            testbed_loader / reporter │
│                                                                      │
│  受測物：部署後 serialwrap（pipx+systemd daemon）＋ COM0/COM1 真板     │
│  遠端對端：docker 容器（sshd；rm-topo 另含 uart_harness 假 daemon）    │
│  暗礁：Windows 原生 serialwrapd.exe（共享 USB 樹 → preflight 探測）    │
└──────────────────────────────────────────────────────────────────────┘
```

### 雙發行單位（release wheel 零改動）

plugin 生態慣例是**開發端 editable install**（wifi_llapi 與 testpilot 本身皆為 `_editable_impl_*.pth`），從不進 release wheel：

- 主 `pyproject.toml` → serialwrap wheel（部署用）——完全不動；realhw 維持 top-level、維持不進 wheel（#122 決策不翻案）。
- `reliability/pyproject.toml` → `serialwrap-reliability` dist（永不 release）：`dependencies=["testpilot-core>=0.3.4,<1.0"]`、`[project.entry-points."testpilot.plugins"] serialwrap_reliability = "serialwrap_reliability.plugin:Plugin"`。
- 安裝：`cd ~/prj_arc/testpilot && uv pip install -e ~/prj_pri/serialwrap/reliability`。
- **editable 的關鍵副作用**：`plugin.py` 的 `__file__` 就在 repo 裡，`parents[2]`＝repo root，`sys.path` 插入後 `import realhw` 零打包技巧。

### 兩個入口、一個引擎

realhw 引擎維持獨立可跑（`python3 -m realhw`，config.json）；plugin 是第二個前端：

```
testpilot run serialwrap_reliability
  → prepare_run()      realhw preflight（六項＋新增）當 gate，不過整場拒跑（仿 wifi_llapi PreflightGate）
  → discover_cases()   realhw REGISTRY → case dicts（tier/destructive/requires 進 metadata；
                       破壞性 tier 預設過濾、--case 顯式點名才進；longrun steps 依 duration/interval 合成）
  → setup_env()        benchlock flock＋testbed.yaml→合成 realhw cfg→建一次 Ctx，per-case 重用
  → execute_step()     black-box：case.run(ctx) → CaseResult{verdict, reason, category, reason_code, evidence}
  → evaluate()         PASS→True；FAIL→False 並填 case["_last_failure"]{category, reason_code, evidence}
  → teardown()         recovery_command() 收板＋清 tmux/docker 殘留
  → core               _classify_diagnostic_status → agent_trace/<case>.json
  → create_reporter()  產 realhw 慣有 report.md/report.json（＋longrun-analysis.md）；report_formats=["md","json"]
```

### 關鍵設計決策

1. **config 單一 loader、雙來源**：`realhw.load_cfg()` 接受 config.json（standalone）或 testbed.yaml 合成 dict（plugin）。bench 機台事實（busid/serial/COM 映射/usbipd 路徑/**Windows 端 serialwrap.exe 路徑**）以 testbed.yaml 為正統，config.json 為 standalone 後備；雙來源等價性由單測保證。
2. **執行策略**：`execution_policy` → `{mode: sequential, max_concurrency: 1}`；`agent-config.yaml` 顯式設 `retry.max_attempts: 1`（core 預設 2；重跑對真機 case 無意義且危險）。remediation 設 `enabled: true` **僅作 failure_snapshot 擷取通道**（實地考證：core 於 disabled 時不寫 snapshot，所有 FAIL 退化 Inconclusive、分類全滅），實際修復動作鎖死（max_attempts=1 使 on_retry 永不 dispatch；不覆寫 decision hooks＝decision 恆 None，為唯一真正生效的阻擋點——core 對空 `allowed_actions` 集合為 falsy 短路、空白名單不攔截，不可依賴）；`hooks.enabled_hooks` 必含 `on_failure`。
3. **longrun 走 default run_loop**（已驗證 core 的 band 攤平對無 band case 無害）：一個 case、N 個 checkpoint step（discover 時合成）、always-pass criteria 防中斷、判決集中在收尾 evaluate；進度監控看 realhw 增量寫的 `snapshots.ndjson`（testpilot 的 agent_trace 於 case 結束才落盤，非長跑監控來源）。
4. **報告身分**：`--dut-fw-ver` 語意＝**deployed serialwrap 版本**（preflight 自動取自 `serialwrap --version`/daemon status 烙進 meta）；板卡 fw 降為環境 metadata。
5. **benchlock 歸屬 realhw preflight（Phase 1）**：flock（`~/.local/state/serialwrap/bench.lock`）＋pgrep 偵測進行中的外部 `testpilot run`——standalone realhw run 同樣受保護，plugin 經 `prepare_run()` 的 preflight gate 天然繼承，不在 plugin 殼重複實作。動機：reliability 與 wifi_llapi 不可同跑（reliability 會 restart daemon/拔 USB）。
6. **plugin 薄殼**：`plugin.py` 只做 glue（繼承 PluginBase）；核心邏輯（case dict 映射、`_last_failure` 抄寫、cfg 合成、benchlock）放**不 import testpilot 的 `core.py`**——serialwrap CI 不裝 testpilot 也能單測。

## 4. Case 目錄（36 cases＝既有 29 條（其中 2 條修訂）＋新增 7 條）

### 既有 29 case 原樣繼承

P0×8、P1 console×7、P1 cmd×3＋wal×2、P1 restart×4⚡、handoff×2⚡＋hotplug×2⚡、longrun lr-mixed×1。plugin 端 case id 不變。

### 既有 case 修訂

| case | 修訂 |
|---|---|
| `p1-hp-cycle`⚡ | 內建 **Windows 端自動救援**：usbipd attach 回插失敗 → `WinSwCli` 探測 Windows serialwrapd 是否持有該裝置 → Windows 端 `device release` → 重試 attach（≤2 次）。救援失敗才 FAIL＝`FailEnv/windows_daemon_holds_device`＋attended 標記。「hp 除外」從永久排除降級為 fallback。 |
| `p1-rst-reboot`／`p1-rst-bootwindow`⚡ | 設計零改動；部署版本 <0.2.3（缺 #130 autoboot guard）時 SKIP＝`FailEnv/deployed_daemon_stale`（capabilities family-gate）。 |

### 新 remote 族（tier `remote`，7 條，全部非破壞性）

第一層 `rm-topo-*`——搬 harness 拓樸（容器封閉世界、假 UART，驗這台機的隧道工具鏈）。實作＝給 `remote_tunnel_test.sh` 加逐拓樸分派參數後 shell out；exit code＋log 尾段 → verdict＋evidence：

| id | 內容 |
|---|---|
| `rm-topo-direct` | direct `-R` expose＋`-L` connect＋close/prune 全流程（容器 daemon） |
| `rm-topo-nat-host` | NAT→host relay＋攻擊者容器隔離斷言 |
| `rm-topo-dual-nat` | 雙 NAT relay＋兩側繞行隔離斷言 |
| `rm-topo-gwports` | GatewayPorts/`--remote-socket` fail-closed＋teardown 複查 |

第二層 `rm-live-*`——**部署 daemon＋真板**（harness 做不到、realhw 存在的意義；容器只當 ssh 對端）：

| id | 內容 |
|---|---|
| `rm-live-e2e` | host `-R` expose 到容器 → 容器內 agent 穿隧道 `session list`＋`cmd submit COM0 echo <marker>` → 真板回 marker、WAL source 歸因正確 → close → registry/log 清空、無孤兒 ssh、**live daemon pid 全程不變** |
| `rm-live-orphan` | 開隧道 → `kill -9` ssh 進程 → `remote status` 觸發 prune/orphan-scan → 狀態檔清乾淨 → 重開成功（自癒） |
| `rm-live-cycle` | 連續 open/close ×5 → 每輪 status 正確、registry 不累積、daemon 零觸碰 |

`requires`：`rm-topo-*`→`docker`；`rm-live-*`→`docker`＋`two_boards`＋`remote_capability`。不滿足→執行期 SKIP（plugin 側＝FailEnv＋reason）。

## 5. 分類映射

### Schema 增強（realhw 側，向後相容）

```python
@dataclass
class CaseResult:
    verdict: str                 # PASS / FAIL / SKIP（不變）
    reason: str = ""
    category: str = ""           # 新增：environment | session | configuration | test
    reason_code: str = ""        # 新增：自由字串，進 trace 供診斷，不影響分桶
    evidence: dict = ...
```

plugin `evaluate()` 把 FAIL/SKIP 的 `category`/`reason_code` 抄進 `case["_last_failure"]`，core 的 `_classify_diagnostic_status` 映射 diagnostic_status（詞彙與映射在 core，plugin 只標 tag）。standalone realhw report.md 同步多一欄分類。

### 映射總表

| realhw 結果 | category | → diagnostic_status | 代表 reason_code |
|---|---|---|---|
| PASS | — | **Pass** | — |
| FAIL（case 內斷言不過，**預設**） | `test` | **FailTest** | `console_fanout_lost`／`defer_interleaved`／`wal_integrity`／`raw_ownership_not_granted`／`daemon_died`／`rss_leak`／`tunnel_state_leak`／`daemon_touched_by_remote` |
| FAIL（外因，case 明確偵測） | `environment`（或 `session`，同映 FailEnv；本套件因受測物反轉原則上少用 session） | **FailEnv** | `windows_daemon_holds_device`／`usbipd_device_lost`／`docker_unavailable`／`sshd_unavailable` |
| FAIL（部署版本不足） | `environment` | **FailEnv** | `deployed_daemon_stale`／`remote_capability_missing` |
| FAIL（bench 設定錯） | `configuration` | **FailConfig** | `testbed_board_mismatch`／`win_serialwrap_path_invalid`／`invalid_case_config` |
| FAIL（未捕捉例外） | *(空)* | **Inconclusive** | `uncaught_exception`——誠實承認分不清 |
| SKIP（執行期前置不滿足） | `environment` | **FailEnv** | `base64_missing`／`broken_by:<case-id>` |

### SKIP 雙軌處置

- **選擇期排除**（破壞性未 opt-in、tier 未選）→ `prepare_run()` 過濾，**不進 run、不出現在報表**。
- **執行期 SKIP**（跑到才發現前置不滿足）→ 映射 FailEnv＋reason_code，誠實入帳。

### 歧義裁決策略

case **一律假設板卡健康**（preflight 兩板 READY＋case 間恢復檢查已保證）：case 內斷言失敗**預設 `test`**；板卡真的死掉由 case 間恢復檢查抓到 → 後續 case SKIP＝`FailEnv/broken_by`。單一裁決線，逐案不猶豫。

### lr-mixed 分類

收尾 evaluate 讀 longrun-analysis：`daemon_death_at` 非 None→`test/daemon_died`；RSS 超閾值→`test/rss_leak`；快照斷流→`environment/snapshot_gap`；重大事件停跑→依事件種類；乾淨跑完→Pass。checkpoint step 本身 always-pass。

逐案精確 reason_code 清單（36 案 × 各失敗點）屬實作細節，plan 階段逐案定；本 spec 定分桶規則與裁決線。

## 6. Preflight／錯誤處理

### 檢查總表（兩級判決）

| 檢查 | 級別 | 內容 |
|---|---|---|
| 既有六項（部署新鮮度／doctor／兩板 READY／工具鏈／環境乾淨／破壞性預告） | **suite-refuse** | 照舊，任一不過整場拒跑 |
| `benchlock`（新） | **suite-refuse** | flock＋pgrep 偵測外部 `testpilot run`——拿不到鎖或偵測到 wifi_llapi run 即拒跑 |
| `windows_daemon`（新） | **診斷增強** | `WinSwCli` 探測 Windows 端 serialwrapd：存在＋持有清單烙進 meta；持有目標 busid 時把「兩板 READY」的 fail 歸因到 `windows_daemon_holds_device` |
| `capabilities`（新） | **family-gate** | `remote` 子命令存在性、部署版本 ≥0.2.3、docker 可達——缺項不擋整場，只讓宣告 `requires` 的 case 執行期 SKIP＝FailEnv＋reason |
| `docker`（新，隨 capabilities） | **family-gate** | docker CLI＋daemon 可達；image 建置延遲到第一個 rm-topo case（harness 自有 image 邏輯，build log 當 evidence） |

版本身分同時烙印：deployed `serialwrap --version`、repo HEAD sha、板卡 fw 寫進 run meta。

### 執行期錯誤處理鏈（由內而外四層）

1. **case 內**：斷言失敗→FAIL＋category/reason_code＋evidence；未捕捉例外→harness 兜底 FAIL＝Inconclusive＋`uncaught_exception`。
2. **case 間恢復**（既有 `recovery_command` state-aware 分派）＋**hp 救援鏈**：
   ```
   usbipd attach 失敗
     → WinSwCli 探測 Windows 端是否持有該裝置
       → 有：Windows 端 device release → usbipd attach 重試（≤2 次）
       → 救回：case 照常收尾（救援過程記 evidence）
       → 救不回：FAIL=FailEnv/windows_daemon_holds_device（attended 標記）
                → 後續依賴 case 走 broken_by SKIP（=FailEnv/broken_by:<id>）
   ```
3. **case teardown 保證**：tmux kill（既有）＋rm 族 docker 收尾——沿用 harness `teardown_now`＋trap，wrapper 在 finally 再掃一次同名容器/network；rm 族每案結尾 `remote close all`＋斷言 state dir 淨空。
4. **run 級**：benchlock 隨進程結束自動釋放（flock 天性）；longrun 重大事件停跑保留現場（既有）。

### 營運前置（plan Task 0）

首次 plugin run 前 redeploy 0.2.3+remote（`install.sh --system --with-sudo`＋`sudo systemctl restart serialwrap`；注意 setup `transitioned:false` 不自動重啟的坑）——「部署新鮮度」check 過了才算完成。順帶解鎖 rst-reboot/bootwindow 與 COM1 brcm 破壞性複驗。

## 7. 測試策略

### 第一層：realhw 純邏輯單測（`tests/test_realhw_*.py`，CI 全跑）

| 對象 | 測法 |
|---|---|
| `CaseResult` 新欄位 | 預設空、report render 帶分類欄、向後相容 |
| capabilities 判斷 | 版本比較、`requires`→SKIP 映射（preflight evaluate 純函式模式擴充） |
| `WinSwCli` 解析 | Windows 端 JSON 輸出 → 持有清單，餵固定字串 |
| hp 救援鏈決策 | **純決策函式**（注入探測結果 → 回傳 action 序列），subprocess 薄層分開 |
| rm-topo verdict 映射 | exit code＋log 尾段 → CaseResult 分桶 |
| testbed loader | **雙來源等價性**：同 bench 事實，config.json 與 testbed.yaml 合成相同 cfg dict |
| longrun steps 合成 | duration/interval → N checkpoints 邊界 |

### 第二層：plugin 殼單測

核心邏輯在不 import testpilot 的 `core.py` → serialwrap CI 直接測；`plugin.py`（繼承 PluginBase 的 glue）只在 bench 整合驗；benchlock 兩進程搶鎖單測。

### 第三層：bench 真機驗收（漸進五步）

1. **standalone 先綠**：redeploy 後 `python3 -m realhw --tier remote`＋`--only p1-hp-cycle`（救援鏈實戰）＋補驗解鎖的 rst-reboot/bootwindow。
2. **plugin 冒煙**：editable install → `testpilot list-plugins`→`list-cases` 36 條→`run --case p0-doctor` 通 agent_trace/diagnostic_status/報表全鏈。
3. **雙前端一致性（adapter 正確性最強驗收）**：同一部署上 standalone 與 plugin 各跑 P0＋P1 非破壞性，逐案 verdict 必須一致；不一致即歸因（adapter bug vs 真機偶發）後重跑確認，歸因不出即視為 adapter 缺陷。
4. **分類落桶驗證**：停 docker→FailEnv、testbed 寫錯 busid→FailConfig、真實 FAIL→FailTest。
5. **longrun 短跑**：checkpoint 版 `--duration 15m` 驗步進模型＋reporter；48h 只在重大進版前動用。

### CI／policy 影響

serialwrap CI 只多跑新純邏輯單測；`reliability/` 不進 CI matrix（dev-only dist）；R-09 fragment、R-16/R-18 docs 對齊照 policy。

## 8. 風險與緩解

| 風險 | 緩解 |
|---|---|
| testpilot API 演進破壞 plugin | `api_version="1.1"` pin（core 同 major 且 minor ≥ plugin 才載入）；plugin 薄殼把面縮到最小 |
| core run_loop 的 wifi band 攤平耦合 | 已實證：`bands` 缺席時欄位為無害裝飾，`overall_case_status` 邏輯正確；若未來 core 改動 → fallback `create_runner()` |
| 雙 daemon 暗礁再咬（Windows 端搶裝置） | preflight `windows_daemon` 探測＋hp 救援鏈＋`windows_daemon_holds_device` 歸因 |
| 部署落後 repo 造成假 FAIL | capabilities family-gate＋`deployed_daemon_stale` reason＋報告身分烙 deployed 版本 |
| 與 wifi_llapi 撞 bench | benchlock suite-refuse（單邊實作已足以保護 reliability run；wifi_llapi 側共用留 v2） |
| harness 全容器世界與 rm-live 環境干擾 | rm-topo 與 rm-live 用不同容器名前綴＋各自 teardown；rm-live 不碰 harness 的 SERIALWRAP_RUN_DIR |
