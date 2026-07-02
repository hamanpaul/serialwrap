# 設計：實機穩定性測試套件（realhw stability suite，#122）

- 日期：2026-07-02
- Issue：#122
- 分支：`feature/122-realhw-stability-suite`
- 狀態：使用者已核可方案 A（獨立 Python harness，2026-07-02）；報告落 `~/b-log/realhw-reports/`；長跑預設 32h、獨立 tier、手動觸發

## 背景與目標

每次重大更新部署後，需在本機（有 production daemon＋兩塊真板）驗證實機穩定性。歷來實機驗證程序散落在各 issue/PR 的驗證紀錄與 agent 記憶（本設計的 case 目錄由三路彙整去重而來：專案記憶 22 條＋跨 session 知識庫 20 條＋repo 文件/coexist E2E 32 條）。本套件把它們制度化為**手動觸發、無人在場**的自動化 suite：P0 煙霧（~15 分鐘）、P1 核心穩定性、長跑（預設 32h、可獨立指定/排除），並產出機器＋人可讀報告。

與歷來「throwaway daemon 驗新碼」的差異：**本套件測的是部署後系統**——用已安裝的 `serialwrap` CLI 操作 live daemon 與真板（部署驗收），不是在沙箱裡跑 repo 程式碼（開發期驗證）。throwaway 隔離法保留為 P2 手動程序的前置手法。

## 範圍

**納入（全自動）**：P0 煙霧、P1 核心穩定性（console 對抗／重啟恢復／裝置交接／usbipd 插拔／命令執行／WAL）、長跑（無人看護，log 齊全＋事後分析報告）。

**排除**：MCU 燒錄（需特定環境：FTDI MCU 線＋韌體＋GPIO BSL——保留為 P2 手動程序）、CI 執行、自動排程（全套手動觸發）、web dashboard、打包進 pipx wheel。

## 架構

```
realhw/                      # repo 根新目錄；不入 wheel；pytest tests/ 不收集
├── __main__.py              # CLI：python3 -m realhw
├── harness.py               # case registry、preflight、執行引擎、報告產生
├── drivers.py               # swcli / tmuxctl / usbipd / systemd 四個薄包裝
├── config.yaml              # 機器特定組態（板卡 by-id、usbipd 路徑與 busid、tmux 前綴）
└── cases/
    ├── p0.py                # P0 煙霧（依序）
    ├── p1_console.py        # console 對抗（tmux+minicom）
    ├── p1_restart.py        # 重啟恢復
    ├── p1_handoff.py        # 裝置交接
    ├── p1_hotplug.py        # usbipd 插拔
    ├── p1_cmd.py            # 命令執行
    ├── p1_wal.py            # WAL
    └── longrun.py           # 長跑
```

### CLI 介面

```
python3 -m realhw --tier p0              # 只跑 P0
python3 -m realhw --tier p0,p1           # 部署後標準組合
python3 -m realhw --tier longrun --duration 32h   # 長跑（獨立 tier；--duration 預設 32h，接受 2h/45m/3600s）
python3 -m realhw --only p1-con-fanout   # 單跑一條 case
python3 -m realhw --tier p1 --skip p1-rst-reboot  # 排除特定 case
python3 -m realhw --list                 # 列出全部 case（id/tier/title/破壞性標記）
python3 -m realhw --report-dir <dir>     # 覆寫報告位置（預設 ~/b-log/realhw-reports/<YYMMDD-HHMMSS>/）
```

長跑不含在 `--tier p1` 內，必須顯式 `--tier longrun`（使用者情境：放假/下班前放下去跑）。

### 核心資料形

- `Case`：`id`、`tier`（p0|p1|longrun）、`title`、`destructive: bool`（reboot/restart/hotplug 類）、`requires`（tmux/usbipd/sudo/two_boards…）、`hints: list[str]`（診斷提示，失敗訊息附上）、`run(ctx) -> CaseResult`。
- `CaseResult`：`verdict`（PASS|FAIL|SKIP）、`reason`、`evidence: dict`（命令與輸出、capture-pane 快照、檔案路徑）、`duration_s`。
- `Ctx`：config、report dir、drivers 實例、per-case evidence 收集器、前後 daemon snapshot（`session list` JSON）。

### Drivers（薄包裝，皆 subprocess、stdlib-only）

- `swcli`：呼叫已安裝 `serialwrap`（PATH 解析），JSON 輸出解析、exit code、`--socket` 不帶（打 live daemon 是本套件的目的）。
- `tmuxctl`：`new-session`／`send-keys -l`／`capture-pane -p`／`kill-session`，session 名帶 config 前綴＋run timestamp（避免撞既有 session）。
- `usbipd`：包 config 指定的 `usbipd.exe` 完整路徑（非互動 shell PATH 找不到）；`list` 解析 busid↔serial 映射（**每次跑前重新 list**——busid 換線會變，不可寫死）；`detach -b`／`attach -w -b`。
- `systemd`：`sudo -n systemctl restart|stop|start serialwrap`、`systemctl show -p ActiveState,MainPID`。

### Preflight（fail-fast，任一不過整場拒跑）

1. 部署新鮮度：repo `git fetch` 後比對 `origin/main`（落後即警告並列 commit 數；`serialwrap --version` 記錄進報告）。
2. `serialwrap doctor` 綠（含 `single_daemon`）。
3. 兩板 READY 且 by-id serial 與 `config.yaml` 相符。
4. 工具可用：`tmux -V`、`usbipd.exe list`、`sudo -n true`。
5. 環境乾淨：無 `sw-coexist-*`／throwaway daemon 殘留、無其他 pytest 在跑、live state.json 無 `/tmp/sw-*` 污染哨兵。
6. 印出本輪將執行的破壞性動作清單（依選中的 case 的 `destructive` 標記彙整：板卡 reboot／daemon restart／usbipd 插拔），與報告目錄路徑。

### 執行語意

- Case 固定順序（檔內宣告序）、彼此獨立、**continue-on-failure**；單 case 失敗記 evidence＋診斷提示後繼續。
- 每 case 前後抓 daemon snapshot；case 結束若 session 狀態被自己弄髒（如 RELEASED 未收回），case 自身負責還原（`finally` 收尾），harness 於下一 case 前驗兩板 READY，不 READY 則嘗試一次 `device attach`／等待恢復，仍失敗 → 後續依賴板卡的 case 記 SKIP（reason=前置不滿足）。
- 破壞性 case（reboot/restart/hotplug）排在各檔案尾端，減少對後續 case 的擾動面。

### 報告

- `report.json`：run metadata（版本、git SHA、時間、tier、環境快照）＋逐 case（verdict/reason/duration/evidence 相對路徑）。
- `report.md`：摘要表（PASS/FAIL/SKIP 計數＋逐 case 一行）＋失敗案例段（reason＋診斷提示＋evidence 連結）。
- evidence 落 `<report-dir>/<case-id>/`（命令 log、capture-pane 文字、相關 WAL/b-log 檔路徑引用）。

## Case 目錄

> 步驟欄為核心動作與判定；完整命令、等待節奏與坑在實作計畫逐 case 展開。「⚡」＝destructive。

### P0 煙霧（tier=p0，~15 分鐘）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p0-doctor` | daemon 與環境健康 | `doctor` 全綠；`session list` 兩板 READY、by-id 符合 config、`console_count` 合理 |
| `p0-cmd-async` | agent 命令端到端 | `cmd submit --cmd 'echo P0_$RANDOM' --cmd-timeout 12` → 隔一拍 `cmd status` 取回 marker（坑：立刻讀有 line race；雙板序列化送） |
| `p0-console-raw` | minicom 連線＋raw ownership | tmux 起 `serialwrap-minicom COM0` → `console-list` 見 client 且 `interactive_owner:true` → send-keys 半個命令＋Tab → capture-pane 斷言補完回顯（掉 line-buffer＝`[A` 症狀） |
| `p0-clear-reattach` | session clear 自動恢復 | `session clear` → 輪詢 ~10s 內回 READY |
| `p0-selftest` | self-test 基本判讀 | READY 板 `self-test` → classification=OK、`probe_ok=true`、`command_capable=true` |
| `p0-blog-clean` | b-log 純淨度（ANSI 回歸） | 開關一次 minicom 產生 `mini_COM*_*.log` → 無 `Script started` 標頭、ESC(0x1b) 計數=0 |
| `p0-wal-live` | WAL 活性與位置 | 戳 `echo X` → `~/.local/state/serialwrap/wal/raw.mirror.log` mtime 跳動且 grep 到 X（勿讀 stale `~/b-log/raw.*`） |
| `p0-multiopen` | 無多開 | `daemon status` 的 `multi_open=false`、`foreign_holders` 空 |

### P1 console 對抗（tier=p1；tmux+minicom，coexist T 系列實機化）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-con-fanout`（T6） | RX fan-out | minicom 掛著 → agent 依序 submit 3 個 marker → capture-pane 依序全見 |
| `p1-con-defer`（T7） | suspend/deferred/resume | send-keys 半行不按 Enter → agent submit 立即完成（<5s）→ 補 Enter → deferred 輸入自成一行不與 agent byte 交錯 |
| `p1-con-busy` | human_active gating | send-keys 輸入後（active 窗內）agent `interactive-open` → 回 `SESSION_INTERACTIVE_BUSY` |
| `p1-con-softpreempt` | 閒置降級 | human 閒置 >60s（`HUMAN_ACTIVE_WINDOW_S`）→ agent `interactive-open` 回 `soft_preempted:true` → close 後 owner 還原 |
| `p1-con-liveness` | 死亡偵測 | `pgrep -x minicom` 取 PID → `kill -9`（勿 pkill -f self-match）→ 下一 tick `console_count` 回落、`human_attached=false` |
| `p1-con-orphan`（#76） | 孤兒回收＋自癒 | kill -9 後重開 minicom → 不需 restart 即拿回 raw ownership（Tab 生效）；短暫 flap（<3s grace）不掉 line-buffer |
| `p1-con-second` | 第二 console line-buffer | 開第二個 minicom → 第一個保持 raw、第二個走 line-buffer（本地回顯＋行編輯） |

### P1 重啟恢復（tier=p1）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-rst-daemon` ⚡ | restart 不變式 | 趁安靜（log-start/stop 驗 0 byte）`sudo systemctl restart` → 兩板 READY＋COM↔by-id 不對調（#100）＋profile 不漂移（#95 sticky/`profile_source`）＋MainPID 變更 |
| `p1-rst-reboot` ⚡ | RECOVERING 自動恢復 | agent submit `reboot` → 狀態序列 READY→RECOVERING→（自動 relogin）READY，全程無人工；掛一個 console 驗不斷線（坑：prplOS reboot 立刻回 prompt、1-3s 後才真的下去） |
| `p1-rst-bootwindow`（#69/#94） ⚡ | 開機窗自動重探 | reboot 後開機窗內 `session clear`＋`attach` → 誠實回報（非致命 error_code）→ 輪詢 `reprobe_attempts` 遞增 → RX 轉閒後自動 READY（live profile timeout_s=10s 可能卡不住窗——卡不住時降級斷言：無人工介入下最終 READY 且 `reprobe_attempts>=0` 記錄實況） |
| `p1-rst-recover` | recover TIMEOUT 複檢 | `session recover` 回 TIMEOUT/ok:false 時 → 立刻 `self-test` 複檢 `probe_ok=true`＋`bridge_generation` 遞增＝實已成功（契約行為，非失敗） |

### P1 裝置交接（tier=p1，#54）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-ho-cycle` ⚡ | release→外部佔用→收回 | `device release --selector COM1 --source agent:realhw` → state.json `released` 有持久化 → tmux 起 `minicom -D <real_path>`（外部持有者）→ `daemon status` `foreign_holders` 列出＋`device attach` 回 `DEVICE_STILL_HELD` → 關外部 minicom → `device attach` 收回 READY |
| `p1-ho-persist` ⚡ | RELEASED 跨 restart | release → `sudo systemctl restart` → session 仍 RELEASED（daemon 不搶回）→ attach 收回 |

### P1 插拔（tier=p1，usbipd 模擬 hotplug）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-hp-cycle` ⚡ | 同板拔插回原槽 | `usbipd detach -b <busid>` → session 轉 DETACHED（保留）→ `attach -w -b` 同板 → 拿回原 COM、自動 ATTACHING→READY；另一板全程不受擾 |
| `p1-hp-reorder` ⚡ | 反序插拔＋restart COM 不對調（#100） | 兩板 detach → 反序 attach（real_path 翻轉）→ DETACHED-rebind 各回原 COM → 再 `sudo systemctl restart` → startup rank 下 COM↔by-id 仍不變 |

### P1 命令執行（tier=p1）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-cmd-modes` | 三模式基本盤 | line 框定／background 長輸出 `result_tail` 逐段讀／interactive open→send（`--encoding key`）→close；錯誤面：非 READY 回 `SESSION_NOT_READY` |
| `p1-cmd-serial` | 多 agent 序列化 | 5 條並行 loop 各以 `--source agent:N` 三輪 submit 唯一 marker → 逐筆 stdout 無 cross-talk → `wal export` 驗 source 歸屬與計數守恆 |
| `p1-cmd-file` | 檔案傳輸＋RPC 不凍結 | `file push`/`file pull` md5 校驗；傳輸中迴圈 `health.ping` 量延遲不被卡（#52；歷史病灶 19.8s） |

### P1 WAL（tier=p1）

| id | 驗什麼 | 核心步驟／判定 |
|---|---|---|
| `p1-wal-reset`（T1/T2/T3） | reset 契約 | console 掛著 → `wal reset` → console 不斷線且見後續 marker；`current-seq` 歸零後再遞增；RPC seq 與 WAL 檔尾一致 |
| `p1-wal-fullrun`（T8） | orchestrator 模擬 | reset → 3 輪 case（seq 嚴格遞增）→ `wal export --from-seq 0` 有記錄 → console 全程存活見全部 marker |

### 長跑（tier=longrun，`--duration` 預設 32h）

`lr-mixed`：4 個 agent worker（兩板輪流、line/background/interactive 混合、唯一 marker）＋1 模擬 human（tmux+minicom，週期 send-keys）＋每 5 分鐘快照（`session list`／`daemon status`／daemon RSS/PID）。**無人看護**：case 級異常記錄後繼續；**daemon 死亡或兩板同時長時間非 READY**＝重大事件 → 記錄、停止負載、保留現場（不自動重啟）。結束（時間到／SIGINT）產 `longrun-analysis.md`：狀態轉換時間線、各 source 命令 submitted/done/error 計數、卡 ATTACHED 事件清單（歷史主要退化模式）、資源趨勢、與歷史基線（32h/31 segments/submitted 49,899）對照。

### P2 手動程序（不自動化；checklist 文件收錄索引與程序）

- MCU flash `/dev/ttyMCU`（#55：throwaway 隔離＋GPIO BSL 逐行短指令＋`ocp-mcu-upgrade`＋三個實機-only 回歸坑）
- U-Boot uboot-template／bootloader recovery lease（#44：spam space 攔 autoboot、大寫 `U-Boot> ` prompt）
- self-test classification 全譜（拔線/login:/REBOOTING/HUMAN_INTERACTIVE/PASSTHROUGH/RELEASED 各情境）
- 安裝/監管模式轉換（on-demand↔systemd）、Windows loopback 驗證
- func-test YAML 13 案例實機化（future work）

## 文件交付

`docs/func-test/realhw-stability-checklist.md`：人可讀完整清單——P0/P1 逐 case 對照 id 與手動等效命令、P2 手動程序全文、前置作業（部署/清潔/throwaway 通則）、坑一覽。suite 是它的可執行形式，checklist 是它的權威描述與手動 fallback。

## 測試策略（TDD 對象）

harness 純邏輯進 `tests/`（正常單元測試、不碰 live）：case registry 與 tier/only/skip 過濾、`CaseResult`→report.json/md 產生、usbipd `list` 輸出解析（busid↔serial）、tmux capture 斷言 helper（marker 尋找、ANSI 剝除）、長跑分析器（吃合成快照 log 產統計）、duration 解析（`32h`/`45m`）。實機 case 本身不進 tests/（其正確性由實機執行本身驗證）。

## 風險與已知限制

- 板卡 reboot／hotplug 類 case 天然受板子開機時間影響，等待上限要寬（per-case timeout 進 config）；失敗訊息附「還在開機」判別提示（看 `last_rx_at`）。
- `p1-rst-bootwindow` 在 live profile（timeout_s=10s）下可能卡不住開機窗——採降級斷言並在報告記錄實況，不視為 FAIL。
- usbipd busid 換線會變：每輪跑前 `list` 重解析，config 只存 serial→期望 COM 映射。
- suite 與 live guard（#120）的關係：本套件**不在** pytest tests/ 下、不載入其 conftest；它對 live 的操作是目的而非污染。反向地，跑本套件期間不得同時跑 `pytest tests/`（live guard 會把本套件的操作當 FAIL 抓——preflight 檢查無其他 pytest）。
- 長跑期間 daemon 死亡不自動重啟是刻意設計（保留現場優先於統計連續性）；歷史 32h 基線的 controller 會重啟並分段統計，本套件第一版簡化為「停止並保留」，分段統計列 future work。

## YAGNI（不做）

自動排程、CI、web dashboard、MCU 燒錄自動化、打包進 wheel、跨機器泛化（config.yaml 綁本機兩板環境）、長跑自動重啟分段統計（v1 簡化）。
