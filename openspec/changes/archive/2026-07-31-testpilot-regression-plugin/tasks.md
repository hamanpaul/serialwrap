# Tasks: testpilot-regression-plugin

## 1. 骨架與 harness（主 agent，契約敏感）

- [x] 1.1 realhw `drivers.SwCli` 加可注入 exe 參數（預設 `"serialwrap"` 不變）＋既有測試不破
- [x] 1.2 `regression/pyproject.toml`（dist `serialwrap-regression`、entry-point `testpilot.plugins`）＋package 骨架（`__init__.py`）
- [x] 1.3 `serialwrap_regression/harness.py`：自有 `Case`（family/issues 欄位）dataclass＋registry＋register＋select；重用 realhw `CaseResult`
- [x] 1.4 `serialwrap_regression/ctx.py`：`RegCtx`（pinned SwCli＋TmuxCtl＋report_dir＋note）
- [x] 1.5 `serialwrap_regression/guards.py`：`UBootConsole`（interrupt_autoboot／readonly_cmd 白名單／leave）＋`ensure_ready`＋`ThrowawayDaemon`
- [x] 1.6 `serialwrap_regression/preflight.py`：重用 realhw collect/evaluate/benchlock＋版本對齊 gate＋venv stale client 記入 notes；工具檢查裁剪（無 usbipd）
- [x] 1.7 `serialwrap_regression/core.py`：registry 載入、case dict、blackbox run、分診 payload、runtime skip（destructive gate／能力缺失）
- [x] 1.8 `serialwrap_regression/plugin.py`：PluginBase 薄殼（三大契約陷阱處理）＋`reporter.py`＋`agent-config.yaml`＋`testbed.yaml.example`（serialwrap_exe／allow_destructive／timeouts／boards）

## 2. Family cases（cortex→agy gemini-3.6-flash-high 實作、cg review；順序＝執行序）

- [x] 2.1 F3 失敗可觀測性（#94 #16 #124）：非空 error_code/stderr、log tail 預設最新、缺裝置指名 selector
- [x] 2.2 F1 命令契約（#23 #27 #129 #19 #15）：limits 查詢、CMD_TOO_LONG、newline 拒收、近上限不登出、cmd_id 不消失
- [x] 2.3 F5 console 共存（#78 #7 #8 #42 #11 #53）：多輪並發 suspend/resume raw ownership、deferred flush、對端消失回收、stale pts
- [x] 2.4 F6 RPC 不凍結（#80 #52）：長操作中 health.ping、雙板互不阻塞
- [x] 2.5 F2 背壓（#81 #128）：SESSION_QUEUE_FULL、history/capture 淘汰＋RSS、recovery flush
- [x] 2.6 F4 狀態語義（#34 #26 #28）：activity 分類、background status/result_tail 一致、interactive 中 line cmd
- [x] 2.7 F7 檔案傳輸（#21 #32）：binary md5 round-trip、大檔不截斷、缺 base64 SKIP
- [x] 2.8 F8 daemon 單一性（#101 #53）：multi_open/foreign_holders、throwaway 第二 daemon 偵測
- [x] 2.9 F9 開機/U-Boot（#69 #130 #44 #14 #20，destructive）：autoboot 窗無 probe、quiet window 不擋 human/agent、attach 自動 reprobe、U-Boot 停留不踢 human、recover 語義、收尾 READY
- [x] 2.10 F10 登入帳密（#140 #19，destructive）：CREDENTIALS_UNRESOLVED 終態不送空帳密、補帳密後 attach、bcm 可診斷錯誤

## 3. 單測（pytest）

- [x] 3.1 `tests/test_regression_harness.py`：registry 載入、id 唯一、issues 必填、與 realhw registry 不互染
- [x] 3.2 `tests/test_regression_guards.py`：U-Boot 白名單逐禁令釘住、ensure_ready 邏輯、ThrowawayDaemon env 組裝
- [x] 3.3 `tests/test_regression_preflight.py`：版本 gate 純函式（齊／不齊／解析失敗）、destructive gate skip 判定
- [x] 3.4 `tests/test_regression_pluginfiles.py`：plugin 檔案存在性、agent-config 契約鍵值、testbed 載入

## 4. 文件與政策

- [x] 4.1 README「TestPilot 回歸測試」中英雙語章節（安裝／執行／destructive gate／何時跑）
- [x] 4.2 `docs/regression-plugin.md`：family↔issue 對照、testbed 說明、U-Boot 護欄、「從新修好的 bug 新增 case」SOP
- [x] 4.3 CLAUDE.md 條件式回歸 case 政策＋`.github/pull_request_template.md` checklist 加項

## 5. ops 與驗證

- [x] 5.1 testpilot venv serialwrap 0.2.1→0.2.4 升級＋#154 留言記錄
- [x] 5.2 editable 安裝 plugin 進 testpilot venv、`testpilot list-plugins`／`list-cases` 煙霧
- [x] 5.3 `python3 -m pytest -q tests/` 無新失敗＋`python3 -m policy_check --repo .`（帶 PR 參數）
- [x] 5.4 sonnet subagent 真機全案實測：非破壞集＋`allow_destructive: true` 全跑（bench 空檔、會 reboot 兩板），全 PASS／合理 SKIP 才放行

## 6. 收尾

- [x] 6.1 `changelog.d/155-testpilot-regression-plugin.md` fragment
- [x] 6.2 openspec archive（注意：archive 觸發 R-22、新 spec Purpose 不得留 TBD）
- [x] 6.3 commit（繁中 Conventional Commits＋Copilot trailer）、push、PR（`Closes #155`、填 checklist）
