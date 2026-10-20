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
- **bench 互斥**：regression、reliability 與 wifi_llapi 共用 `bench_lock_path`。Linux 未設定時使用 `~/.local/state/serialwrap/bench.lock`；Windows 必須設定可由所有測試套件共同開啟的 lock 路徑。若測試跨越 WSL 與 Windows，兩邊檔案鎖互不相通；請在 regression 根組態或 reliability 的 `serialwrap_reliability` 區段設定 `cross_os_probe_argv` 執行另一側唯讀 `serialwrap session list`。probe 回報裝置被持有時 preflight 拒跑；無法確認時記錄 `unknown`，由擁有者排程避免重疊（OD-10）。
- **Windows 能力式執行**：Windows preflight 檢查 pinned CLI、daemon／doctor／WAL／板卡 READY、版本及 bench lock。缺少 `tmux`、`usbipd`、systemd、POSIX throwaway daemon 或 POSIX PTY 孤兒回收能力，會讓相依 case 以 `cap_missing:<capability>` 記為 SKIP；不相依的 case 照常執行。`human_frontend: auto` 在 Windows 選 loopback Telnet frontend，因此一般 F5 console、U-Boot、realhw console 與長跑 human 輸入可使用 Telnet；POSIX PTY 孤兒語意案例會能力式 SKIP。#224 的 WAL range 模式 `truncated=true` 代表**還有下一頁**，不是紀錄遺失。
- **背景啟動注意**：preflight 以平台行程快照掃描 pytest、serialwrapd 與 testpilot；Windows 使用 ToolHelp 與 CIM／WMIC 命令列資料。相關行程的命令列無法取得時會回報證據不足並拒跑；`testpilot.exe run` 也會辨識為進行中的測試。

## testbed 組態

預設讀 `regression/serialwrap_regression/testbed.yaml.example`；同目錄放 `testbed.yaml` 即覆蓋（不入版控）。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `serialwrap_exe` | `~/.local/bin/serialwrap` | **pinned 部署版 CLI**。case 一律走此絕對路徑，不吃 venv PATH（#154 防線） |
| `bench_lock_path` | Linux：`~/.local/state/serialwrap/bench.lock`；Windows：必填 | regression、reliability 與 wifi_llapi 共用的跨工具 bench lock |
| `human_frontend` | `auto` | `auto` 在 POSIX 選 tmux/minicom，在 Windows 選 loopback Telnet；亦可指定 `tmux` 或 `telnet` |
| `cross_os_probe_argv` | `[]` | regression 根組態與 reliability 的 `serialwrap_reliability` 區段皆可設定；輸出須有 `sessions` 陣列，依實體 serial／`device_by_id` 比對，不依賴 COM 編號；持有時拒跑，無法確認時記錄守衛狀態（OD-10） |
| `allow_destructive` | `false` | `true` 才實跑 F9（reboot）／F10（throwaway 交接）；否則記 SKIP（`destructive_gated`） |
| `boards` | COM0=prpl、COM1=bcm | bench 板卡（com／alias／serial／platform） |
| `timeouts.ready_wait_s` | 180 | 收尾等 READY 上限 |
| `timeouts.boot_wait_s` | 240 | reboot 後等開機上限（F9） |
| `timeouts.cmd_timeout_s` | 12 | 一般命令逾時 |
| `timeouts.wal_oracle_s` | 10.0 | WAL oracle 判定（F12／`p0-wal-live`）等待 marker 逾時上限 |
| `tmux_prefix` | `swreg` | console case 的 tmux session 前綴（收尾自動掃除） |

## preflight（suite-refuse 條件）

doctor 全綠、testbed 板卡 READY、無殘留 throwaway daemon、無並行 pytest、state 無污染哨兵、benchlock 取得、無外部 testpilot run，以及——

- **版本 gate（#154）**：pinned CLI `--version` 與 daemon 版本（優先讀 `daemon status` 的 `version` 欄位；欄位缺席才落回讀 daemon pid 的 venv `importlib.metadata`）不一致 → 整場拒跑。
- PATH 上解析到的 serialwrap 若與 pinned 不同版（如 venv stale client），記入 preflight notes（不擋——本 plugin 不使用它）。
- **平台能力**：`platform_missing_caps` 會依目前平台與 pinned tool 路徑，把工具缺項交給 case `requires` 做能力式 SKIP；Windows 不以全域平台條件略過所有 case。選到 `human_frontend: telnet` 時不需要 tmux/minicom。

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
| F5 console 共存 | #78 #7 #8 #42 #11 #53；Stage 0 待補（`f5-deferred-dropped-after-reboot`，見下節） | raw ownership 多輪 suspend/resume 不丟、deferred 不丟鍵、對端消失回收、不掛 stale pts；reboot（上下文失效）後舊按鍵不回放（BC-02） | **部分（reboot case）** |
| F6 RPC 不凍結 | #80 #52 | 長操作中 `daemon status` 往返不凍結、雙板互不餓死 | 否 |
| F7 檔案傳輸 | #21 #32 #161 #166 | binary round-trip md5 一致、不靜默截斷；可用 base64 或 OpenSSL 搭配 md5sum，不因只缺 base64 就 SKIP；明確 `CHECKSUM_MISMATCH` 一律為 `FAIL/test/binary_roundtrip_mismatch`，不受工具探測值影響；`PULL_PARSE_FAILED`、`MOVE_FAILED`、逾時等未完成傳輸維持環境 `SKIP`；push 預設走 #161 echo-ACK 節流（echo 停滯獨立 reason_code `transfer_echo_stall`） | 否 |
| F8 daemon 單一性 | #101 #53 | `multi_open`／`foreign_holders` 正確回報第二 daemon 與 tty 持有者 | 否 |
| F9 開機/U-Boot／BRCM 登入 | #69 #130 #139 #44 #14 #20 #224 | autoboot 窗不被 probe 打斷、quiet window 不擋 human／真 READY 後的 agent 命令、自發重開機過渡態 agent 命令被 `AUTOBOOT_QUIET` 攔下（#139）、開機中 attach 自動 reprobe、U-Boot 停留不踢 console；BRCM 重開後帶前導空白的 BDK prompt 能自動登入、切 `sh` 並回 READY；passthrough 真板 attach 零 TX、靜默 30 秒升 READY 並以 `--completion-regex` 完成（SC-35，`pt-quiet-ready`，見下節待補表） | **是（reboot；pt-quiet-ready 為 throwaway＋release）** |
| F10 登入帳密 | #140 #19 | 帳密解析空→`CREDENTIALS_UNRESOLVED` 終態不送空帳密、補帳密後恢復 | **是（throwaway＋release）** |
| F10 裝置交接（`f10_device_handoff.py`） | Stage 0 待補（`f10-release-drain-receipt`、`f10-release-aborts-inflight-command`，見下節） | release 的 ok 回條只在 handle 關閉後送出（BC-04／SC-04）；在途命令於 release 後 `COMMAND_ABORTED_IN_FLIGHT` 且 TX 不重送（BC-03） | **是（throwaway＋release）** |
| F11 RX 洪水/傳輸層 | #153 #150 | 洪水下 probe 失敗分類為 `RX_FLOOD`（等排空、排空後自癒回 READY）、`rx_bytes_last_10s`／`last_rx_age_s`／`last_tx_age_s`／`last_error_detail` 觀測面存在且合理（真 usbip stall 需人工操作、排除自動化，heuristic 由 pytest 覆蓋） | **部分（flood case 佔用 COM0 數十秒）** |
| F12 診斷保真 | #154 #148 | `daemon status` 直接帶 `version` 欄位（不靠 `/proc cmdline + importlib.metadata` 繞路）、且與 pinned CLI `--version` 一致——版本欄位悄悄消失時 preflight 的 fallback 會靜默接手掩蓋，僅此 case 斷言快路徑本身；`daemon status` 的 `wal_path` 真實存在且送一筆命令後讀回含 marker 之新增 WAL 紀錄並驗證 CRC 判定活性（非死路徑）、`doctor` 報告含 `wal_dir` 檢查項（#148） | 否 |

執行順序＝F3→F1→F5→F6→F2→F4→F7→F8→F12→F9→F10→F11（破壞性壓軸；F11 flood 殿後避免殘 log 污染他 family 基線）。

### Stage 0 待補 issue 的 case（`pending_issue`）

Stage 0 行為變更（BC-02、BC-03、BC-04／SC-04、SC-35）的實機 case 已撰寫，但擁有者尚未為它們開立 issue。依 Stage 0 計畫（T21）規定：

- `Case(pending_issue=True)` 且 `issues=()`，待擁有者開立 issue 號後改填 `issues=("#NNN",)` 並把 `pending_issue` 移除。**不得**填假號，**不得**自行開 issue。
- `pending_issue` 與 `issues` 互斥；`register()` 只對 `pending_issue=True` 的 case 接受空 `issues`，其餘 case 仍拒絕空 `issues`（#155）。
- case 中繼資料輸出 `pending_issue` 欄位，報表可辨識待補項目。

| case id | family | 行為 | 實跑入口 | 狀態 |
|---|---|---|---|---|
| `f10-release-drain-receipt` | F10 | `device release` 的 ok 回條只在 handle 關閉後送出（外部 `open(tty)` 成功時間 ≤ 回應時間，否則 FAIL `release_receipt_before_handle_close`） | TestPilot（`allow_destructive: true`）；非 root 執行 | BLOCKED-OWNER（Stage 0 bench） |
| `f10-release-aborts-inflight-command` | F10 | 長命令在途時 release，`COMMAND_ABORTED_IN_FLIGHT`；同 cmd_id 的 TX 重複出現 → FAIL `resend_detected` | 同上 | BLOCKED-OWNER |
| `f5-deferred-dropped-after-reboot` | F5 | agent 命令在途時鍵入的舊按鍵，於 reboot（上下文失效）後不回放；對照組（未 reboot）須回放，否則 SKIP `insufficient_evidence` | tmux＋minicom（`requires: tmux`），`destructive=True` | BLOCKED-OWNER |
| `pt-quiet-ready` | F9 | passthrough（真板 U-Boot／MCU 線）attach 至 READY 全程 WAL 零 TX、READY 不早於靜默 30 秒下限，`cmd submit --completion-regex` 完成（SC-35）；板子最後觀測仍有 RX 時判 SKIP `rx_not_quiet`，WAL 證據缺漏判 SKIP `insufficient_evidence` | throwaway（explicit target 釘定 passthrough profile 綁 COM0，sandbox by-id 只放該線；無動態偵測、無 PROBE）＋prod `device release`／`attach` 收尾，`destructive=True` | BLOCKED-OWNER（bench 實跑；OD-14 對應 issue 待擁有者開立，`issues=()` 於 PR 說明） |

執行順序以 case id 排序（`harness.ordered()`）：`f10-release-*` 落在既有 `f10-creds-*` 之後、`f10-unresolved-*` 之前，屬 F10 family 內的 id 排序結果；破壞性 F10 case 彼此皆以 throwaway＋`device release`／`attach` 收尾。

Stage 0 實機程序與腳本（非 TestPilot case，手動執行，各檔首段含 `entry`、`revision`、`expected`、`owner`、`evidence_dir`、`rollback`）：

- `realhw/procedures/stage0-release-kill.md`：release 持久化前後 kill（`SERIALWRAP_TEST_KILL_AFTER_SAVE=1` 僅於 release 落盤後、隔離實例生效；`SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE=<目錄>` 提供 commit 前的 `arrived` 暫停點）。
- `realhw/procedures/stage0-windows-remote-topologies.md`：Windows remote 三拓樸（BC-05、OD-12 信任範圍）。
- `realhw/procedures/stage0-spike1-win-vcom.md`：Spike 1 Windows 虛擬 COM 資格（需管理員，OD-01／OD-08 退路）。
- `scripts/stage0/spike3_win_wal.py --list`：Spike 3 四個 case（`win-wal-av-rotate`、`win-wal-av-unlink`、`win-wal-av-reader-handle`、`win-wal-f12-progress`）；於 Windows host 以 `py -3.12` 執行，證據 JSON 依 `scripts/stage0/spike3_evidence.schema.json` 驗證（EDR 欄位必填）。非 Windows host 執行只產出 `INSUFFICIENT_EVIDENCE`，不得作為證據。
- `scripts/stage0/drain_timing.py`：Linux 與 Windows 實體 USB-UART 依序量測 release 耗時分佈與 3,000 ms 逾時次數。

## U-Boot 唯讀護欄（F9 硬約束）

由 `guards.UBootConsole` 在 harness 層強制（單測釘死），case 無法繞過：

- **允許**：中斷 autoboot、白名單唯讀命令（`printenv`、`bdinfo`、`version`、`help`、`echo`）、以 `reset`（預設，完整硬重開）或 `boot` 離開。**脫困與收尾一律優先 `reset`**（operator 裁示 2026-08-01）：`boot` 只從當前 bootloader 狀態續走，做過互動後續走的狀態不保證乾淨。
- **一律拒送（raise）**：`saveenv`、`env save`、`env default`、`setenv`（任何變數）、flash 寫入（`sf`/`nand`/`mmc write`）、`tftpboot`、任何串接（`;`／換行／`&&`／`|`）。
- 每個 F9 case 收尾必經 `guards.ensure_ready`——板子回 READY 才回報 verdict，不得留在 U-Boot prompt。

## 目前已知紅燈／SKIP（2026-07-31 首日三輪實測基準）

以下保留歷史 bench 結果，不是 #166 候選的實機驗收。#166 增加工具 fallback 及
profile 行長預算後，F7 需使用對應版本重新執行；受影響板須先依量測設定
`max_console_line_chars`，不能用舊的「缺 base64」SKIP 證明新 fallback 不可用，
也不能以本地 shell／pytest 結果宣稱真板已轉綠。

| case | 狀態 | 原因 |
|---|---|---|
| `f4-background-result-tail-consistent` | **預期 PASS**（#159 已修，常駐回歸防線；若重現紅燈須經擁有者核准後依 §11.4 登記 XFAIL 哨兵，含 issue、失敗特徵、期限） | #159：background quiet-window 對快速完成命令整段吞輸出且回 `lost: False`；case 曾正確抓到此產品缺陷，**勿改 oracle 遷就**。修復後 case 轉綠並常駐。Stage 0 bench 實跑結果屬 BLOCKED-OWNER（TST-D-6） |
| `f7-binary-roundtrip-md5`／`f7-larger-file-not-truncated` | 逐板嘗試；兩板皆 SKIP `target_tool_missing`（誠實判定） | **#166 根因翻案**：傳輸層無罪（手工 base64 往返 md5 完全一致），**兩塊 bench 板都沒有 `base64`**（PATH 與 busybox applet 皆無；`md5sum`／`openssl` 有）——協定硬依賴 `base64` 才是根因。先前 COM0「工具齊全」是探測 sentinel 誤判（命令本文含 `TOOLS_OK`、回顯即恆真，已改用 `TOOLS_$((6*7))`）。另附帶：prpl console 有 **505 字元單行上限**（超過即靜默截斷、status 仍 done），是 chunk 512 撞 `TRANSFER_ECHO_STALL` 的原因 |

#156（recover CTRL_C 路徑不 flush 佇列）已修——根因＝`SessionManager` 無管道通知
`CommandArbiter` flush，新增 `on_command_flush` callback 補上該分支（`session_manager.py`
→ `service.py` → `arbiter.py` `flush_session()`）：`f2-recovery-flushes-queue` 已收緊為嚴格
斷言（10s 有界排空，非原本放寬的 30s）。#158（快速迴圈偶發 PROMPT_TIMEOUT）已修——根因＝
RX 視窗修剪破壞 offset 語意（絕對偏移記帳根治）：`f2-history-bounded-rss` 已收緊為零容忍
（任一輪未 done 即 FAIL），並新增決定性重演 case `f2-rx-window-crossing-prompt`。其餘 issue
修復後可收緊對應 oracle。
| `f9-spontaneous-reboot-agent-gated` | 常駐防線（#162 已修，預期綠） | 原 #162 紅燈哨兵：連續重開機後延遲 banner 污染後續命令 stdout（quiet 清空判定過早）。#162 修復＝quiet 清空改綁 READY 再確認（`ready_reconfirm_pending`＋reprobe 確認 probe 消耗 askconsole banner），`_wait_quiet_cleared` 已加等 pending 歸 False；本 case 轉為常駐防線，再紅即回歸 |
| （bench 事實）COM1 bcm 板 | — | busybox 映像未編入 `base64` applet——F7 逐板 fallback 的「bcm 實證 #157」客觀上不可行（`target_tool_missing` 為真實板端限制，非探測 bug；`md5sum` 存在）|


## 部署後置檢查（實機環境）

`serialwrap doctor` 的 **`profile_bootloader_prompts`**（advisory）會比對線上 `~/.config/serialwrap/profiles/default.yaml` 與出貨資產 `sw_core/assets/profiles/default.yaml`。**舊部署環境的線上 profile 不會自動跟隨新資產更新**——2026-08-01 實測本 bench 的 `prpl-template` 就缺 `bootloader_prompts`，導致 `BOOTLOADER` 分類、recovery lease 授予、boot-quiet 的 bootloader 守衛整條 no-op（daemon 卡在 bootloader 時給不出任何可行動訊號）。

修法：`serialwrap setup` 重新物化資產，或備份後手動把出貨資產對應 template 的 `bootloader_prompts` 段落補進線上檔，restart daemon 後確認該檢查轉綠。**新部署環境或升級後請一併檢查。**

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
