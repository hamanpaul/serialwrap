# Proposal: realhw-stability-suite-122

## Why

每次重大更新部署後需要在本機（production daemon＋兩塊真板）驗證實機穩定性，但歷來實機驗證程序散落在各 issue/PR 驗證紀錄與 agent 記憶中，每次都要重新考古、人工照表操課且無報告留存（#122）。三路彙整已去重出完整 case 目錄，需要把它制度化為手動觸發、無人在場的自動化套件。

## What Changes

- 新增 `realhw/` 獨立目錄（不入 wheel、不被 `pytest tests/` 收集）：`python3 -m realhw` CLI（`--tier p0|p1|longrun`、`--only/--skip`、`--duration`、`--list`、`--report-dir`）＋harness（registry/preflight/執行引擎/報告）＋drivers（swcli/tmuxctl/usbipd/systemd 薄包裝）＋cases（P0×8、P1×20、longrun×1）＋機器組態 `realhw/config.yaml`。
- Preflight fail-fast：部署新鮮度、doctor、兩板 READY、工具可用（tmux/usbipd/sudo）、環境乾淨、破壞性動作清單預告。
- 報告：`~/b-log/realhw-reports/<ts>/` 下 `report.json`＋`report.md`＋per-case evidence；長跑另產 `longrun-analysis.md`（無人看護、事後分析）。
- 新增 `docs/func-test/realhw-stability-checklist.md`：人可讀完整清單（P0/P1 對照 case id＋P2 手動程序：MCU flash、U-Boot、self-test 全譜、安裝轉換、Windows）。
- harness 純邏輯的單元測試進 `tests/`（registry 過濾、報告產生、usbipd 解析、capture 斷言 helper、長跑分析器、duration 解析）。

**不改動任何 serialwrap runtime 行為**（sw_core 零變更）。

## Capabilities

### New Capabilities
- `realhw-stability-suite`: 部署後實機穩定性驗證套件——tier 化 case 執行（P0 煙霧/P1 核心/longrun）、preflight 守門、continue-on-failure 與 evidence 收集、JSON+Markdown 報告、長跑無人看護與事後分析。

### Modified Capabilities

（無——不動既有 runtime capability 的 requirement。）

## Impact

- 新增：`realhw/`（~8 檔）、`docs/func-test/realhw-stability-checklist.md`、`tests/test_realhw_*.py`（harness 純邏輯單測）。
- 不動：`sw_core/**`、既有 tests、CI（本套件不進 CI）。
- 相容性注意：套件操作 live daemon 是目的；與 #120 live guard 互斥——跑本套件期間不得同時跑 `pytest tests/`（preflight 檢查）。
- 執行環境依賴：本機兩板（by-id serial 寫入 config.yaml）、tmux、usbipd-win、NOPASSWD sudo；缺任一 → preflight 拒跑。
