# serialwrap_regression testpilot plugin 設計（#155）

日期：2026-07-31｜狀態：已核准（brainstorm 四項裁示＋設計核准）｜關聯：#155、#154、PR #145（serialwrap-reliability）

## 一句話定位

以 TestPilot 為殼，把 serialwrap **已 CLOSED 且有實際修正**的 bug 寫成實機回歸 case，在真 bench 上常跑（改動後／發版前），防止回歸。

## 與 serialwrap-reliability 的區隔（沿 #155 定案）

| | serialwrap-reliability（已交付） | serialwrap_regression（本案） |
|---|---|---|
| 問的問題 | 跑久了會不會壞？ | 以前修好的，有沒有壞回去？ |
| case 來源 | 穩定性場景設計（realhw 29 cases） | 已修 issue（10 個 Scenario Family） |
| 單輪時長 | 小時級（含 48h） | 分鐘～十幾分鐘級 |
| 分診 | serialwrap＝受測物 | 同左（失敗→FailTest） |

兩 plugin 刻意分開、不合併；共用 realhw harness 基礎設施。

## 裁示記錄（2026-07-31）

1. **交付範圍：全 10 family 一次交付**（F1–F10，含破壞性 F9/F10 與 P2 F7/F8）。
2. **#154 前置：三重防線都做**（pin 路徑＋版本 gate＋ops 升級 venv client）。
3. **派工：cortex→agy `gemini-3.6-flash-high`**（agy 失敗 fallback `claude --model haiku`）；**cg `--effort high` review**。cortex 遇 bug → 去 cortex repo 開 issue；cortex 走不下去 → 由主 agent 開 sonnet subagent 接手實作。
4. **CLAUDE.md 規則：條件式評估**（非一律強制）。

另沿用 #155 issue 內既有裁示：testpilot run 期間兩條 UART 保留給測試專用（console 可自由開關）；reboot／進 U-Boot 放行，**唯板上設定值不得更動**；throwaway daemon 不算動到本機設定；#12（32h 長跑）排除歸 reliability。

## 架構決策

**平行薄殼、重用 realhw 引擎**（#155 傾向）：新開獨立 package，與 `reliability/` 平行；自有 case registry，import realhw 的 `drivers`（TmuxCtl、strip_ansi、Systemd、SwCli 模式）與 `preflight`（collect/evaluate/benchlock）。否決塞 realhw 新 tier（兩 plugin 定位不得混）與全新 harness（重造無收益）。

### 套件結構

```
regression/
  pyproject.toml            # hatchling；dist serialwrap-regression
                            # entry-point testpilot.plugins → serialwrap_regression.plugin:Plugin
  serialwrap_regression/
    __init__.py
    plugin.py               # testpilot PluginBase 薄殼（照 reliability 模式）
    core.py                 # registry 載入、case dict 組裝、blackbox run、分診 payload
    ctx.py                  # RegCtx：SwCli(pinned exe)＋TmuxCtl＋report_dir＋note()
    guards.py               # U-Boot 唯讀護欄＋ensure_ready＋ThrowawayDaemon
    preflight.py            # 重用 realhw.preflight＋版本對齊 gate
    reporter.py             # md/json 報告（照 reliability 模式）
    testbed.yaml.example    # 板卡、serialwrap_exe、allow_destructive、timeouts
    agent-config.yaml       # testpilot 契約設定（見下）
    cases/
      f01_cmd_contract.py   f02_backpressure.py   f03_observability.py
      f04_session_semantics.py  f05_console_coexist.py  f06_rpc_liveness.py
      f07_file_transfer.py  f08_daemon_singleton.py
      f09_boot_uboot.py     f10_login_creds.py
```

### Case 模型

自有 `Case` dataclass（copy realhw 小模型＋擴充），registry 為本 package 模組層 list（**不**共用 realhw 全域 REGISTRY，避免互相汙染 discover）：

```python
@dataclass(frozen=True)
class Case:
    id: str                      # 例 "f3-fail-error-code"
    family: str                  # "F1".."F10"
    title: str
    run: Callable[[RegCtx], CaseResult]
    issues: tuple[str, ...]      # 對應已修 issue，例 ("#94", "#16")
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
```

`CaseResult` 直接重用 realhw（verdict/reason/evidence/duration/category/reason_code）。issues 進 case metadata 與報告（「#94 回歸」可追溯）。

### 分診契約（serialwrap＝受測物）

- `category="test"` → FailTest：回歸破功（當初的錯誤行為再現）。
- `category="environment"` → FailEnv：bench／板卡／工具問題。
- `category="configuration"` → FailConfig：testbed 設定錯。
- FAIL 而 category 空 ＝ Inconclusive（避免）。

### testpilot 三大契約陷阱的處理（沿 reliability 實證）

1. remediation `enabled: true`＋`max_attempts: 1`＋`hooks.enabled_hooks` 含 `on_failure`、**不覆寫 decision hooks**（enabled:false 會讓所有 FAIL 變 Inconclusive；`allowed_actions: []` 是 falsy 不攔截）。
2. `retry.max_attempts` 顯式設 1（缺省 core 預設 2）。
3. `execute_step` 一律回 `success=True`，判決集中 `evaluate`（step 失敗會跳過 evaluate）。

## #154 三重防線

1. **pin 路徑**：realhw `drivers.SwCli` 加可注入 exe 參數（預設 `"serialwrap"`，reliability 行為不變）；regression 從 testbed `serialwrap_exe`（預設 `~/.local/bin/serialwrap`）注入，永不吃 testpilot venv PATH。
2. **版本 gate（preflight，suite-refuse）**：pinned CLI `--version` ↔ daemon 回報版本不齊即整場拒跑；venv 內 PATH 解析到的 serialwrap 版本記入 preflight notes（警示不擋——本 plugin 不使用它）。
3. **ops 升級（交付流程步驟，非 repo 程式碼）**：把 testpilot venv 的 serialwrap 0.2.1 升到 0.2.4（與部署 daemon 對齊），在 #154 留言記錄；#154 的「可診斷性」本體不由本案關閉。

## 破壞性隔離與執行順序

- testbed `allow_destructive: false`（預設）時，destructive case 一律 **SKIP**（非 FAIL）——「改動後快跑」只跑非破壞集；發版前設 true 全跑。
- 執行順序：F3→F1→F5→F6→F2→F4→F7→F8→**F9→F10**（破壞性壓軸）；plugin `execution_policy` sequential／max_concurrency 1。

## U-Boot 唯讀護欄（F9 硬約束，harness 層強制）

`guards.UBootConsole` 只暴露三類操作，**其餘一律 raise**（有單測釘住白名單）：

- `interrupt_autoboot()`：autoboot 倒數中送鍵停在 U-Boot prompt。
- `readonly_cmd(cmd)`：白名單唯讀命令（`printenv`、`bdinfo`、`version`、`help`、`echo`）；`saveenv`／`env save`／`env default`／`setenv`／`sf|nand|mmc write`／`tftpboot` 等一律拒送。
- `leave(via="boot"|"reset")`：離開 U-Boot 讓板子開完機。

每個 F9 case 收尾強制 `guards.ensure_ready(ctx, com)`——等板子回 READY，逾時 FAIL 並跑 recovery；**不得把板子留在 U-Boot prompt**。

## ThrowawayDaemon（F10）

`guards.ThrowawayDaemon` context manager：隔離 `SERIALWRAP_RUN_DIR`／`_STATE_DIR`／`_WAL_DIR`／`_BY_ID_DIR`（sandbox by-id 只放目標線）＋自帶 profile；進場前 prod `device release` 交接、離場 kill daemon＋prod reclaim＋等 READY。手法沿用 CLAUDE.md／realhw 既有驗證慣例（背景啟動用純 `nohup &`，避免 exit 144 坑）。

## 10 Family case 清單（issue 對照＋oracle）

oracle 一律＝「當初的錯誤行為不得再現」。預估 29–31 cases。

| Family | 對應已修 issue | 主要 case（oracle 摘要） | 破壞性 |
|---|---|---|---|
| F1 命令契約〔P0〕 | #23 #27 #129 #19 #15 | limits 可查詢（`max_submit_cmd_bytes` 等）；超長→`CMD_TOO_LONG` 不卡死；含換行→`CMD_CONTAINS_NEWLINE`；近上限合法命令執行後不登出；accepted `cmd_id` timeout 後仍查得到 | 否 |
| F2 背壓〔P1〕 | #81 #128 | 超過 pending 上限→`SESSION_QUEUE_FULL`；history／capture 有淘汰、RSS 不線性成長；recovery 後佇列清空不連鎖 | 否 |
| F3 失敗可觀測性〔P0〕 | #94 #16 #124 | 任何失敗 CLI：stdout JSON 有非空 `error_code`＋stderr 有具體一行；`log tail-*` 預設取最新段；缺裝置錯誤指名 selector | 否 |
| F4 狀態語義〔P1〕 | #34 #26 #28 | `session activity` 分得出 quiet-healthy／真沒反應；background `status`/`result_tail` 分段一致不漏不重；interactive 中 line cmd 行為明確 | 否 |
| F5 console 共存〔P0〕 | #78 #7 #8 #42 #11 #53 | raw ownership 經多輪並發 suspend/resume 不得永久丟失；deferred buffer 不丟鍵；對端消失須回收；bridge 重建後不掛 stale pts | 否（佔 console 可還原） |
| F6 RPC 不凍結〔P1〕 | #80 #52 | 長操作／傳檔中 `health.ping` 可回應；另一板命令不餓死 | 否 |
| F7 檔案傳輸〔P2〕 | #21 #32 | binary（tar.gz 含 null byte）push→pull md5 一致；大檔不靜默截斷；板缺 `base64` → SKIP 非 FAIL | 否 |
| F8 daemon 單一性〔P2〕 | #101 #53 | `daemon status` 的 `multi_open`/`foreign_holders` 正確；throwaway 第二 daemon 可被偵測 | 否 |
| F9 開機/U-Boot〔P0〕 | #69 #130 #44 #14 #20 | reboot 後 3s autoboot 窗內 daemon 不送 system probe、板子不卡 `=>`；quiet window 不擋 human bytes／agent 顯式命令；開機中 attach 失敗須自動 reprobe 終回 READY；U-Boot 停留不踢 human console；reboot 後 recover 語義可靠；收尾必回 READY | **是（reboot）** |
| F10 登入帳密〔P1〕 | #140 #19 | 帳密解析空→`CREDENTIALS_UNRESOLVED` 終態、不送空帳密敲 login、reprobe 不重試；補帳密後 attach 正常；bcm 登入路徑有可診斷錯誤 | **是（throwaway＋release）** |

bench 基準（#155 盤點）：COM0=prpl（U-Boot 2024.04、autoboot 3s 已實證）、COM1=bcm；只測這兩平台，shell/passthrough/Airoha 情境不納入。COM1 的 U-Boot 具備性於首次 reboot 時確認，沒有就該 case 對 COM1 SKIP。

## preflight（重用＋擴充）

重用 realhw：doctor 全綠、板卡 READY、工具、殘留 daemon、other-pytest 互斥、state 污染哨兵、**benchlock**（與 reliability／wifi_llapi 共用 `~/.local/state/serialwrap/bench.lock`，bench 互斥整場拒跑）、外部 testpilot run 偵測。新增：**client↔daemon 版本對齊 gate**（上述防線 2）。usbipd 不需要（本 plugin 無插拔 case）——工具檢查表對應裁剪。

## 報告

沿 realhw／reliability 模式：md＋json，落 `~/b-log/regression-reports/tp-<ts>/`；報告表格含 family、issues 欄（回歸可追溯）；PASS/FAIL/SKIP 統計。

## 文件與政策

- **README.md**（中英雙語對照）：新增「TestPilot 回歸測試」章——venv 安裝（`~/.local/share/testpilot/.venv/bin/pip install -e regression/`）、`testpilot run serialwrap_regression`／`--case <id>`、`allow_destructive` gate、何時跑（改動後快跑非破壞集；發版前全跑）。
- **docs/regression-plugin.md**：family↔issue 對照、testbed.yaml 說明、U-Boot 護欄、**「從新修好的 bug 新增 case」SOP**。
- **CLAUDE.md 新規則（條件式評估）**：修復 bug issue 時必須評估回歸測試歸屬——(a) pytest/mock 可覆蓋→加 pytest；(b) 需實機才驗得到（時序／USB 列舉／真實登入／外部工具搶 tty 等）→必須在 `regression/` 新增 testpilot case（掛 issue 編號）；PR 描述須記錄評估結論（加了哪個 case，或為何免加）。
- **.github/pull_request_template.md**：checklist 加「回歸 case 評估已記錄」。

## 測試（pytest，進 tests/）

`tests/test_regression_*.py`（慣例同 reliability）：guards 白名單（U-Boot 禁令一一釘住）、版本 gate 純函式、registry 載入與唯一性、分診 payload、testbed 載入、plugin 檔案存在性。實機行為不進 pytest（那是 plugin 本體的工作）。

## 交付流程

1. spec（本檔）→ openspec-propose → writing-plans。
2. 骨架（plugin.py/core/ctx/guards/preflight/reporter＋SwCli upstream 小改）由主 agent 實作——契約敏感、踩坑成本高。
3. **10 family cases：cortex 派工 agy `gemini-3.6-flash-high`（fallback `claude --model haiku`）逐 family 實作，`cg --effort high` review**；主 agent 整合。cortex 遇 bug→cortex repo 開 issue；走不下去→sonnet subagent 接手。
4. pytest（無新失敗）＋policy_check（帶 PR 參數複現 CI）。
5. **sonnet subagent 真機全案實測**：bench 空檔（無 wifi_llapi／reliability run），`testpilot run serialwrap_regression` 非破壞集＋`allow_destructive: true` 全跑（會 reboot 兩板），全 PASS（或 SKIP 有正當理由）才可 push。
6. changelog fragment `changelog.d/155-testpilot-regression-plugin.md`、openspec-archive、push、PR（`Closes #155`）。

## 風險與緩解

- **cortex 治理不到本 repo**：降級直呼 agy/cg（同模型），PR 註明；cortex bug 開 issue 到 cortex repo。
- **agy 產出品質**：cg review＋主 agent 整合把關；契約敏感層（plugin.py/guards）不派工。
- **F9 把板子留在 U-Boot**：護欄強制 `leave`＋`ensure_ready`；實測排 bench 空檔。
- **與 prod daemon 互擾**：非破壞 case 全走現行 prod daemon 正常 RPC（與人類共存本來就是產品承諾）；F10 走 throwaway＋release，收尾 reclaim。
- **flaky**：實機時序 case 設計時給足 settle（沿 realhw hints 的 line-race／foreground-busy 教訓）。
