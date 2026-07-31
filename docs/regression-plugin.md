# serialwrap_regression——已修 bug 的實機回歸 TestPilot plugin（#155）

> 定位一句話：**以 TestPilot 為殼，把已 CLOSED 且有實際修正的 issue 寫成實機回歸 case，常跑防回歸。**
> 與 `serialwrap-reliability`（穩定性／soak，#122）刻意分開：那邊問「跑久了會不會壞？」，這邊問「以前修好的，有沒有壞回去？」

## 安裝與執行

```bash
# 一次性：裝進 TestPilot venv（editable、dev-only；release wheel 零改動）
~/.local/share/testpilot/.venv/bin/pip install -e regression/

testpilot list-plugins                    # 應出現 serialwrap_regression
testpilot list-cases serialwrap_regression
testpilot run serialwrap_regression                            # 全套（destructive 依 gate 記 SKIP）
testpilot run serialwrap_regression --case f3-fail-error-code  # 單一 case
```

- **何時跑**：改動 sw_core／daemon 行為後（非破壞集，分鐘級）；發版前（含 destructive 全跑）。
- **報告**：`~/b-log/regression-reports/tp-<ts>/report.md`／`report.json`，每條列 family、對應 issues、分類與 evidence 連結。
- **bench 互斥**：與 reliability／wifi_llapi 共用 benchlock（`~/.local/state/serialwrap/bench.lock`），他者持鎖即整場拒跑。
- **背景啟動注意**：preflight 的外部 testpilot 偵測用 `pgrep -af 'testpilot ru[n]'`——若你的包裝行程（如 `bash -c "... testpilot run ..."`）cmdline 含該字面字串會**自我匹配拒跑**；背景跑請用不含該字串的 launcher script（首輪實測實證）。

## testbed 組態

預設讀 `regression/serialwrap_regression/testbed.yaml.example`；同目錄放 `testbed.yaml` 即覆蓋（不入版控）。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `serialwrap_exe` | `~/.local/bin/serialwrap` | **pinned 部署版 CLI**。case 一律走此絕對路徑，不吃 venv PATH（#154 防線） |
| `allow_destructive` | `false` | `true` 才實跑 F9（reboot）／F10（throwaway 交接）；否則記 SKIP（`destructive_gated`） |
| `boards` | COM0=prpl、COM1=bcm | bench 板卡（com／alias／serial／platform） |
| `timeouts.ready_wait_s` | 180 | 收尾等 READY 上限 |
| `timeouts.boot_wait_s` | 240 | reboot 後等開機上限（F9） |
| `timeouts.cmd_timeout_s` | 12 | 一般命令逾時 |
| `tmux_prefix` | `swreg` | console case 的 tmux session 前綴（收尾自動掃除） |

## preflight（suite-refuse 條件）

doctor 全綠、testbed 板卡 READY、tmux/minicom 存在、無殘留 throwaway daemon、無並行 pytest、state 無污染哨兵、benchlock 取得、無外部 testpilot run，以及——

- **版本 gate（#154）**：pinned CLI `--version` 與 daemon 版本（優先讀 `daemon status` 的 `version` 欄位；欄位缺席才落回讀 daemon pid 的 venv `importlib.metadata`）不一致 → 整場拒跑。
- PATH 上解析到的 serialwrap 若與 pinned 不同版（如 venv stale client），記入 preflight notes（不擋——本 plugin 不使用它）。

## 分診契約（serialwrap＝受測物）

| CaseResult.category | testpilot 分類 | 意義 |
|---|---|---|
| `test` | FailTest | **回歸破功**：當初 issue 的錯誤行為再現 |
| `environment` | FailEnv | bench／板卡／工具問題（含 SKIP 的映射） |
| `configuration` | FailConfig | testbed 設定錯誤 |

注意與 wifi_llapi 相反（那邊 serialwrap 是環境→FailEnv）。SKIP 在 report.md 仍以 SKIP verdict 呈現；testpilot 分診面因無 Skip 分類而落 FailEnv（沿 reliability 慣例）。

## Family↔issue 對照

| Family | 對應已修 issue | 回歸標的 | 破壞性 |
|---|---|---|---|
| F1 命令契約 | #23 #27 #129 #19 #15 | limits 可查、`CMD_TOO_LONG`／`CMD_CONTAINS_NEWLINE` 拒收不卡死、近上限不登出、cmd_id 不消失 | 否 |
| F2 背壓 | #81 #128 #156 #158 | `SESSION_QUEUE_FULL` backpressure、history/capture 淘汰、recovery flush 佇列、RX 視窗飽和跨界 prompt 不失效 | 否 |
| F3 失敗可觀測性 | #94 #16 #124 | 失敗必有非空 `error_code`＋stderr、log tail 預設取最新、錯誤指名 selector | 否 |
| F4 狀態語義 | #34 #26 #28 | activity 分類可區分、background result-tail 不漏不重、interactive 中 line cmd 行為明確 | 否 |
| F5 console 共存 | #78 #7 #8 #42 #11 #53 | raw ownership 多輪 suspend/resume 不丟、deferred 不丟鍵、對端消失回收、不掛 stale pts | 否 |
| F6 RPC 不凍結 | #80 #52 | 長操作中 `daemon status` 往返不凍結、雙板互不餓死 | 否 |
| F7 檔案傳輸 | #21 #32 | binary round-trip md5 一致、不靜默截斷；板缺工具 SKIP | 否 |
| F8 daemon 單一性 | #101 #53 | `multi_open`／`foreign_holders` 正確回報第二 daemon 與 tty 持有者 | 否 |
| F9 開機/U-Boot | #69 #130 #139 #44 #14 #20 | autoboot 窗不被 probe 打斷、quiet window 不擋 human／真 READY 後的 agent 命令、自發重開機過渡態 agent 命令被 `AUTOBOOT_QUIET` 攔下（#139）、開機中 attach 自動 reprobe、U-Boot 停留不踢 console | **是（reboot）** |
| F10 登入帳密 | #140 #19 | 帳密解析空→`CREDENTIALS_UNRESOLVED` 終態不送空帳密、補帳密後恢復 | **是（throwaway＋release）** |
| F11 RX 洪水/傳輸層 | #153 #150 | 洪水下 probe 失敗分類為 `RX_FLOOD`（等排空、排空後自癒回 READY）、`rx_bytes_last_10s`／`last_rx_age_s`／`last_tx_age_s`／`last_error_detail` 觀測面存在且合理（真 usbip stall 需人工操作、排除自動化，heuristic 由 pytest 覆蓋） | **部分（flood case 佔用 COM0 數十秒）** |
| F12 診斷保真 | #154 #148 | `daemon status` 直接帶 `version` 欄位（不靠 `/proc cmdline + importlib.metadata` 繞路）、且與 pinned CLI `--version` 一致——版本欄位悄悄消失時 preflight 的 fallback 會靜默接手掩蓋，僅此 case 斷言快路徑本身；`daemon status` 的 `wal_path` 真實存在且送一筆命令後 mtime 前進（非死路徑）、`doctor` 報告含 `wal_dir` 檢查項（#148） | 否 |

執行順序＝F3→F1→F5→F6→F2→F4→F7→F8→F12→F9→F10→F11（破壞性壓軸；F11 flood 殿後避免殘 log 污染他 family 基線）。

## U-Boot 唯讀護欄（F9 硬約束）

由 `guards.UBootConsole` 在 harness 層強制（單測釘死），case 無法繞過：

- **允許**：中斷 autoboot、白名單唯讀命令（`printenv`、`bdinfo`、`version`、`help`、`echo`）、以 `boot`／`reset` 離開。
- **一律拒送（raise）**：`saveenv`、`env save`、`env default`、`setenv`（任何變數）、flash 寫入（`sf`/`nand`/`mmc write`）、`tftpboot`、任何串接（`;`／換行／`&&`／`|`）。
- 每個 F9 case 收尾必經 `guards.ensure_ready`——板子回 READY 才回報 verdict，不得留在 U-Boot prompt。

## 目前已知紅燈／SKIP（2026-07-31 首日三輪實測基準）

| case | 狀態 | 原因 |
|---|---|---|
| `f4-background-result-tail-consistent` | **FAIL（刻意保留的紅燈哨兵）** | #159：background quiet-window 對快速完成命令整段吞輸出且回 `lost: False`——case 正確抓到產品缺陷，**勿改 oracle 遷就**；#159 修復即轉綠 |
| `f7-binary-roundtrip-md5`／`f7-larger-file-not-truncated` | 逐板嘗試（prpl 環境性失敗→試 bcm；全板失敗才 SKIP `transfer_env_all_boards`） | #157 已修 chunk/timeout 參數面；真機驗收揭露更深根因＝prpl console 無流控節流掉字（512B chunk echo 於 ~73% 斷）＋prpl-template 缺 `timeout_s` 未受益於推導——見 #161（逐行 ACK／流控後續） |

#156（recover CTRL_C 路徑不 flush 佇列）已修——根因＝`SessionManager` 無管道通知
`CommandArbiter` flush，新增 `on_command_flush` callback 補上該分支（`session_manager.py`
→ `service.py` → `arbiter.py` `flush_session()`）：`f2-recovery-flushes-queue` 已收緊為嚴格
斷言（10s 有界排空，非原本放寬的 30s）。#158（快速迴圈偶發 PROMPT_TIMEOUT）已修——根因＝
RX 視窗修剪破壞 offset 語意（絕對偏移記帳根治）：`f2-history-bounded-rss` 已收緊為零容忍
（任一輪未 done 即 FAIL），並新增決定性重演 case `f2-rx-window-crossing-prompt`。其餘 issue
修復後可收緊對應 oracle。
| `f9-spontaneous-reboot-agent-gated` | 常駐防線（#162 已修，預期綠） | 原 #162 紅燈哨兵：連續重開機後延遲 banner 污染後續命令 stdout（quiet 清空判定過早）。#162 修復＝quiet 清空改綁 READY 再確認（`ready_reconfirm_pending`＋reprobe 確認 probe 消耗 askconsole banner），`_wait_quiet_cleared` 已加等 pending 歸 False；本 case 轉為常駐防線，再紅即回歸 |
| （bench 事實）COM1 bcm 板 | — | busybox 映像未編入 `base64` applet——F7 逐板 fallback 的「bcm 實證 #157」客觀上不可行（`target_tool_missing` 為真實板端限制，非探測 bug；`md5sum` 存在）|


## 從新修好的 bug 新增 case（SOP）

> CLAUDE.md「回歸 case 政策」：修 bug issue 時必須評估歸屬——pytest 可覆蓋→pytest；需實機才驗得到→本 plugin。PR 描述記錄評估結論。

1. **判定實機-only**：問「pytest／PTY fake target 抓得到嗎？」抓得到就寫 pytest，到此為止。
2. **選 family**：對照上表；同根因歸入既有 family（`cases/fNN_*.py` 內加 case）；新根因類型才開新 family 檔（`fNN_<slug>.py`，family 需先加入 `harness.FAMILY_ORDER`）。
3. **寫 case**：oracle＝「當初的錯誤行為不得再現」。模板：

   ```python
   @_case("fN-short-slug", "一句話標的", issues=("#NNN",))
   def fN_short_slug(ctx):
       r = ctx.sw.run(...)            # pinned CLI；重要觀測 ctx.note() 進 evidence
       if <當初的錯誤行為再現>:
           return CaseResult("FAIL", reason="...（#NNN 回歸）",
                             category="test", reason_code="specific_code")
       return CaseResult("PASS")
   ```

   慣例：FAIL 必帶 `category`＋`reason_code`；能力缺失（板缺工具、無 U-Boot banner）SKIP 非 FAIL；submit 後隔拍再讀 status（line race）；雙板序列化送（foreground busy）；reboot／交接類標 `destructive=True` 並自行還原（`ensure_ready`）。
4. **單測**：`python3 -m pytest -q tests/test_regression_harness.py`（registry 載入、id 唯一自動涵蓋）。
5. **真機跑過**：`testpilot run serialwrap_regression --case <id>` 綠了才進 PR；PR 描述寫「新增回歸 case `<id>`（#NNN）」。
