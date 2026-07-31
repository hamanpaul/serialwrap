# Proposal: testpilot-regression-plugin

## Why

serialwrap 已有 1300+ pytest（mock/PTY）與 serialwrap-reliability 穩定性套件，但**已修好的實機-only bug 沒有防回歸機制**——這類 bug（真板 boot 時序、USB 列舉、真實登入、外部工具搶 tty）mock 抓不到，修好後只能靠人記得。#154 的 stale client 事故（284 次失敗 282 次訊息全空）更證明缺少「常跑的實機契約檢查」。#155 已完成 62 個 closed issue 的正式盤點與裁示，定案 10 個 Scenario Family。

## What Changes

- 新增 `regression/serialwrap_regression/` testpilot plugin（與 `reliability/` 平行的第二個薄殼；dev-only editable，不進主 wheel）：自有 case registry（Case 含 `family`/`issues` 欄位）、10 個 family 共約 29–31 個實機回歸 case。
- realhw `drivers.SwCli` 加可注入 exe 路徑參數（預設 `"serialwrap"` 不變，reliability 不受影響）——#154 防線之一。
- preflight 新增 client↔daemon 版本對齊 gate（不齊 suite-refuse）；重用 realhw benchlock／doctor／板卡 READY 檢查。
- 新增 U-Boot 唯讀護欄（`UBootConsole` 白名單）與 `ThrowawayDaemon` helper（F10 帳密情境隔離）。
- testbed `allow_destructive` gate：false 時破壞性 case（F9 reboot、F10）SKIP。
- README 新增「TestPilot 回歸測試」中英雙語章節；新增 `docs/regression-plugin.md`（family↔issue 對照、新增 case SOP）。
- CLAUDE.md 新增條件式回歸 case 政策（修 bug issue 必評估：pytest 可覆蓋→pytest；需實機→必加 regression case）；PR template checklist 加項。
- 單測 `tests/test_regression_*.py`（護欄白名單、版本 gate、registry、分診 payload）。

## Capabilities

### New Capabilities
- `regression-testpilot-plugin`: 已修 bug 的實機回歸 testpilot plugin——case registry 與 issue 對照、分診契約（serialwrap＝受測物）、preflight 版本 gate、destructive gate、U-Boot 唯讀護欄、throwaway daemon 隔離、報告產出。

### Modified Capabilities
- `realhw-stability-suite`: `SwCli` 支援注入 serialwrap 執行檔路徑（預設行為不變）。

## Impact

- 新目錄 `regression/`（獨立 hatchling dist，entry-point `testpilot.plugins`）；不動 release wheel。
- `realhw/drivers.py`（SwCli 建構子小改，向後相容）。
- `README.md`、`docs/regression-plugin.md`、`CLAUDE.md`、`.github/pull_request_template.md`、`tests/`。
- 執行期依賴 bench：COM0=prpl、COM1=bcm 兩板、部署版 serialwrap 0.2.4、benchlock 與 reliability／wifi_llapi 互斥。
- ops（非 repo 程式碼）：testpilot venv serialwrap 0.2.1→0.2.4 升級，記錄於 #154。
